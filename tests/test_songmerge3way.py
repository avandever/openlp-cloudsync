"""
Tests for the three-way song merge (:mod:`songmerge` with a merge base).

The base is a snapshot of ``songs.sqlite`` from the last agreed state;
it lets the merge tell "deleted on one side" apart from "added on the
other", so deletions propagate instead of resurrecting.
"""
import sqlite3

import pytest

from openlp.plugins.cloudsync.lib.songmerge import (
    base_identity_keys,
    find_songs_db,
    merge_base_path,
    merge_songs_databases,
    save_merge_base,
)
from tests.openlp_plugins.cloudsync.test_songmerge import make_db, song_titles


def song_a(**overrides):
    song = {'id': 1, 'title': 'Amazing Grace', 'authors': ['John Newton'],
            'lyrics': 'amazing grace how sweet the sound',
            'last_modified': '2026-09-01 00:00:00'}
    song.update(overrides)
    return song


def song_b(**overrides):
    song = {'id': 2, 'title': 'Be Thou My Vision', 'authors': ['Mary Byrne'],
            'lyrics': 'be thou my vision o lord of my heart',
            'last_modified': '2026-09-01 00:00:00'}
    song.update(overrides)
    return song


def base_ids(base_db):
    """The identity keys the base snapshot's uploader had, as a set."""
    return set(base_identity_keys(base_db))


def relation_counts(path):
    connection = sqlite3.connect(str(path))
    try:
        return {
            table: connection.execute('SELECT COUNT(*) FROM {t}'.format(t=table)).fetchone()[0]
            for table in ('authors_songs', 'songs_songbooks', 'songs_topics', 'media_files')
        }
    finally:
        connection.close()


def test_remote_delete_propagates(tmp_path):
    base_db, local_db, remote_db = (tmp_path / name for name in ('base.sqlite', 'local.sqlite', 'remote.sqlite'))
    make_db(base_db, [song_a()])
    make_db(local_db, [song_a()])       # untouched locally
    make_db(remote_db, [])               # deleted on remote

    report = merge_songs_databases(local_db, remote_db, base_db, remote_base_identities=base_ids(base_db))

    assert report['deleted'] == 1
    assert song_titles(local_db) == []
    assert relation_counts(local_db) == {'authors_songs': 0, 'songs_songbooks': 0,
                                         'songs_topics': 0, 'media_files': 0}


def test_local_delete_is_not_resurrected(tmp_path):
    base_db, local_db, remote_db = (tmp_path / name for name in ('base.sqlite', 'local.sqlite', 'remote.sqlite'))
    make_db(base_db, [song_a()])
    make_db(local_db, [])               # deleted locally
    make_db(remote_db, [song_a()])       # untouched remotely

    report = merge_songs_databases(local_db, remote_db, base_db, remote_base_identities=base_ids(base_db))

    assert report['deleted'] == 0
    assert report['added'] == 0
    assert song_titles(local_db) == []


def test_both_deleted_is_noop(tmp_path):
    base_db, local_db, remote_db = (tmp_path / name for name in ('base.sqlite', 'local.sqlite', 'remote.sqlite'))
    make_db(base_db, [song_a()])
    make_db(local_db, [])
    make_db(remote_db, [])

    report = merge_songs_databases(local_db, remote_db, base_db, remote_base_identities=base_ids(base_db))

    assert report['added'] == 0
    assert report['updated'] == 0
    assert report['deleted'] == 0
    assert song_titles(local_db) == []


def test_remote_delete_plus_local_edit_keeps_edit(tmp_path):
    # Data-preserving: the local edit wins over the remote delete.
    base_db, local_db, remote_db = (tmp_path / name for name in ('base.sqlite', 'local.sqlite', 'remote.sqlite'))
    make_db(base_db, [song_a()])
    make_db(local_db, [song_a(lyrics='amazing grace, edited locally',
                              last_modified='2026-09-05 00:00:00')])
    make_db(remote_db, [])

    report = merge_songs_databases(local_db, remote_db, base_db, remote_base_identities=base_ids(base_db))

    assert report['deleted'] == 0
    assert song_titles(local_db) == ['Amazing Grace']
    connection = sqlite3.connect(str(local_db))
    try:
        lyrics = connection.execute('SELECT lyrics FROM songs').fetchone()[0]
    finally:
        connection.close()
    assert lyrics == 'amazing grace, edited locally'


def test_local_delete_plus_remote_edit_readds(tmp_path):
    # Data-preserving: the remote edit wins over the local delete.
    base_db, local_db, remote_db = (tmp_path / name for name in ('base.sqlite', 'local.sqlite', 'remote.sqlite'))
    make_db(base_db, [song_a()])
    make_db(local_db, [])
    make_db(remote_db, [song_a(lyrics='amazing grace, edited remotely',
                               last_modified='2026-09-06 00:00:00')])

    report = merge_songs_databases(local_db, remote_db, base_db, remote_base_identities=base_ids(base_db))

    assert report['added'] == 1
    assert report['deleted'] == 0
    assert song_titles(local_db) == ['Amazing Grace']


def test_no_base_never_deletes(tmp_path):
    # Without a base the merge is the old two-way behavior: additive only.
    local_db, remote_db = (tmp_path / name for name in ('local.sqlite', 'remote.sqlite'))
    make_db(local_db, [song_a(), song_b()])
    make_db(remote_db, [song_a()])      # B "missing" remotely -- but no base, so keep it

    report = merge_songs_databases(local_db, remote_db)

    assert report['deleted'] == 0
    assert song_titles(local_db) == ['Amazing Grace', 'Be Thou My Vision']


def test_missing_base_file_falls_back_to_two_way(tmp_path):
    local_db, remote_db = (tmp_path / name for name in ('local.sqlite', 'remote.sqlite'))
    make_db(local_db, [song_a()])
    make_db(remote_db, [])

    report = merge_songs_databases(local_db, remote_db, tmp_path / 'no-such-base.sqlite')

    assert report['deleted'] == 0
    assert song_titles(local_db) == ['Amazing Grace']


def test_first_upload_without_base_never_deletes(tmp_path):
    # Regression: a first-ever upload from a machine that never synced has
    # no merge base of its own, so its missing songs were never there --
    # they must not be read as "deleted on remote".  (This once wiped 270
    # songs off a desktop when a fresh laptop uploaded its 1-song library.)
    base_db, local_db, remote_db = (tmp_path / name for name in ('base.sqlite', 'local.sqlite', 'remote.sqlite'))
    make_db(base_db, [song_a(), song_b()])
    make_db(local_db, [song_a(), song_b()])
    make_db(remote_db, [song_b(last_modified='2026-09-02 00:00:00',
                               lyrics='be thou my vision, remote words')])

    report = merge_songs_databases(local_db, remote_db, base_db, remote_base_identities=None)

    assert report['deleted'] == 0
    assert song_titles(local_db) == ['Amazing Grace', 'Be Thou My Vision']
    assert any('not in remote backup' in line for line in report['details'])


def test_remote_delete_still_propagates_with_explicit_base_flag(tmp_path):
    # The same shape as the regression above, but the remote uploader HAD a
    # base: then the missing song really was deleted remotely.
    base_db, local_db, remote_db = (tmp_path / name for name in ('base.sqlite', 'local.sqlite', 'remote.sqlite'))
    make_db(base_db, [song_a(), song_b()])
    make_db(local_db, [song_a(), song_b()])
    make_db(remote_db, [song_b()])

    report = merge_songs_databases(local_db, remote_db, base_db, remote_base_identities=base_ids(base_db))

    assert report['deleted'] == 1
    assert song_titles(local_db) == ['Be Thou My Vision']


def test_independent_adds_resolve_by_last_modified(tmp_path):
    base_db, local_db, remote_db = (tmp_path / name for name in ('base.sqlite', 'local.sqlite', 'remote.sqlite'))
    make_db(base_db, [])
    make_db(local_db, [song_a(lyrics='local version', last_modified='2026-09-02 00:00:00')])
    make_db(remote_db, [song_a(lyrics='remote version', last_modified='2026-09-03 00:00:00')])

    report = merge_songs_databases(local_db, remote_db, base_db, remote_base_identities=base_ids(base_db))

    assert report['added'] == 0
    assert report['updated'] == 1
    connection = sqlite3.connect(str(local_db))
    try:
        lyrics = connection.execute('SELECT lyrics FROM songs').fetchone()[0]
    finally:
        connection.close()
    assert lyrics == 'remote version'


def test_remote_only_add_still_added_with_base(tmp_path):
    base_db, local_db, remote_db = (tmp_path / name for name in ('base.sqlite', 'local.sqlite', 'remote.sqlite'))
    make_db(base_db, [song_a()])
    make_db(local_db, [song_a()])
    make_db(remote_db, [song_a(), song_b()])

    report = merge_songs_databases(local_db, remote_db, base_db, remote_base_identities=base_ids(base_db))

    assert report['added'] == 1
    assert song_titles(local_db) == ['Amazing Grace', 'Be Thou My Vision']


def test_save_merge_base_roundtrip(tmp_path):
    data_dir = tmp_path / 'data'
    (data_dir / 'songs').mkdir(parents=True)
    live_db = data_dir / 'songs' / 'songs.sqlite'
    make_db(live_db, [song_a()])

    assert save_merge_base(data_dir) is True
    base_path = merge_base_path(data_dir)
    assert base_path.is_file()

    # The saved base works as a merge base afterwards.
    local_db = tmp_path / 'local.sqlite'
    remote_db = tmp_path / 'remote.sqlite'
    make_db(local_db, [song_a()])
    make_db(remote_db, [])
    report = merge_songs_databases(local_db, remote_db, base_path,
                                   remote_base_identities=set(base_identity_keys(base_path)))
    assert report['deleted'] == 1


def test_save_merge_base_missing_db_returns_false(tmp_path):
    assert save_merge_base(tmp_path / 'empty-data-dir') is False


def test_remote_base_membership_required_for_deletion(tmp_path):
    # v13.4 core case (2026-09-30 12:54 regression): the local base holds
    # A and B, but the remote uploader's OWN base held only B.  Its backup
    # of {B} therefore carries no deletion of A -- A was never on the
    # remote machine, so it must be kept.
    base_db, local_db, remote_db = (tmp_path / name for name in ('base.sqlite', 'local.sqlite', 'remote.sqlite'))
    make_db(base_db, [song_a(), song_b()])
    make_db(local_db, [song_a(), song_b()])
    make_db(remote_db, [song_b()])
    remote_base_db = tmp_path / 'remote-base.sqlite'
    make_db(remote_base_db, [song_b()])

    report = merge_songs_databases(local_db, remote_db, base_db,
                                   remote_base_identities=base_ids(remote_base_db))

    assert report['deleted'] == 0
    assert song_titles(local_db) == ['Amazing Grace', 'Be Thou My Vision']
    assert any('not in remote backup' in line for line in report['details'])


def test_remote_base_identities_is_a_set_not_the_local_base(tmp_path):
    # Even when the remote uploader had a base, deletion needs the song to
    # have been IN THAT base -- a flag-style "remote has a base" is not
    # enough.  Here the uploader's base holds A, so A's absence is a
    # deletion, but B's absence is not.
    base_db, local_db, remote_db = (tmp_path / name for name in ('base.sqlite', 'local.sqlite', 'remote.sqlite'))
    make_db(base_db, [song_a(), song_b()])
    make_db(local_db, [song_a(), song_b()])
    make_db(remote_db, [])
    remote_base_db = tmp_path / 'remote-base.sqlite'
    make_db(remote_base_db, [song_a()])

    report = merge_songs_databases(local_db, remote_db, base_db,
                                   remote_base_identities=base_ids(remote_base_db))

    assert report['deleted'] == 1
    assert song_titles(local_db) == ['Be Thou My Vision']


def test_base_identity_keys_lists_songs(tmp_path):
    db_path = tmp_path / 'base.sqlite'
    make_db(db_path, [song_a(), song_b()])

    keys = base_identity_keys(db_path)

    assert isinstance(keys, list)
    assert len(keys) == 2
    assert keys == sorted(keys)
    assert any('amazing grace' in key for key in keys)
    assert any('be thou my vision' in key for key in keys)


def test_base_identity_keys_empty_when_no_songs(tmp_path):
    db_path = tmp_path / 'empty.sqlite'
    make_db(db_path, [])
    assert base_identity_keys(db_path) == []


def test_base_identity_keys_empty_when_no_songs_table(tmp_path):
    db_path = tmp_path / 'no-table.sqlite'
    connection = sqlite3.connect(str(db_path))
    connection.execute('CREATE TABLE other (id INTEGER PRIMARY KEY)')
    connection.commit()
    connection.close()
    assert base_identity_keys(db_path) == []


def test_find_songs_db_prefers_section_layout(tmp_path):
    (tmp_path / 'songs').mkdir()
    section_db = tmp_path / 'songs' / 'songs.sqlite'
    root_db = tmp_path / 'songs.sqlite'
    make_db(section_db, [song_a()])
    make_db(root_db, [song_b()])
    assert find_songs_db(tmp_path) == section_db


def test_find_songs_db_falls_back_to_root(tmp_path):
    root_db = tmp_path / 'songs.sqlite'
    make_db(root_db, [song_a()])
    assert find_songs_db(tmp_path) == root_db


def test_find_songs_db_returns_none_when_missing(tmp_path):
    assert find_songs_db(tmp_path) is None
