# -*- coding: utf-8 -*-

##########################################################################
# OpenLP - Open Source Lyrics Projection                                 #
# ---------------------------------------------------------------------- #
# Copyright (c) 2008 OpenLP Developers                                   #
# ---------------------------------------------------------------------- #
# This program is free software: you can redistribute it and/or modify   #
# it under the terms of the GNU General Public License as published by   #
# the Free Software Foundation, either version 3 of the License, or      #
# (at your option) any later version.                                    #
#                                                                        #
# This program is distributed in the hope that it will be useful,        #
# but WITHOUT ANY WARRANTY; without even the implied warranty of         #
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the          #
# GNU General Public License for more details.                           #
#                                                                        #
# You should have received a copy of the GNU General Public License     #
# along with this program.  If not, see <https://www.gnu.org/licenses/>. #
##########################################################################
"""
Tests for the Cloud Sync backup library (archive creation, hashing,
change fingerprinting).
"""
import hashlib
import json
import os
import sqlite3
import zipfile

import pytest

from openlp.plugins.cloudsync.lib.backup import (
    _should_exclude,
    compute_fingerprint,
    compute_sha256,
    create_library_archive,
)


@pytest.fixture
def data_dir(tmp_path):
    """A fake OpenLP data directory with a few typical files."""
    (tmp_path / 'songs').mkdir()
    (tmp_path / 'services').mkdir()
    db_path = tmp_path / 'songs' / 'songs.sqlite'
    connection = sqlite3.connect(str(db_path))
    connection.execute('CREATE TABLE placeholder (id INTEGER PRIMARY KEY)')
    connection.commit()
    connection.close()
    (tmp_path / 'services' / 'sunday.osj').write_text(json.dumps({'service': True}))
    (tmp_path / 'notes.txt').write_text('hello')
    return tmp_path


@pytest.fixture
def work_dir(tmp_path_factory):
    """Scratch directory for archive output, outside the fake data dir."""
    return tmp_path_factory.mktemp('work')


def test_compute_sha256_matches_stdlib(data_dir):
    target = data_dir / 'notes.txt'
    assert compute_sha256(target) == hashlib.sha256(b'hello').hexdigest()


def test_create_archive_returns_metadata_with_matching_sha256(data_dir, tmp_path, work_dir):
    archive_path, metadata = create_library_archive(data_dir, work_dir=work_dir)

    assert archive_path.is_file()
    assert metadata['name'] == archive_path.name
    assert metadata['name'].startswith('openlp-library-')
    assert metadata['name'].endswith('.zip')
    assert metadata['sha256'] == compute_sha256(archive_path)
    assert metadata['size_bytes'] == archive_path.stat().st_size
    assert metadata['hostname']
    assert metadata['created_utc']
    assert metadata['file_count'] > 0


def test_archive_contains_library_files(data_dir, tmp_path, work_dir):
    archive_path, _metadata = create_library_archive(data_dir, work_dir=work_dir)

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert 'songs/songs.sqlite' in names
    assert 'services/sunday.osj' in names
    assert 'notes.txt' in names


def test_archive_excludes_noise_files(data_dir, tmp_path, work_dir):
    (data_dir / 'debug.log').write_text('noise')
    (data_dir / 'scratch.tmp').write_text('noise')
    (data_dir / 'cache.lock').write_text('noise')
    (data_dir / 'backup.swp').write_text('noise')
    (data_dir / 'token.json').write_text('{}')
    (data_dir / 'state.json').write_text('{}')
    (data_dir / 'pending-restore.json').write_text('{}')

    archive_path, _metadata = create_library_archive(data_dir, work_dir=work_dir)

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    for excluded in ('debug.log', 'scratch.tmp', 'cache.lock', 'backup.swp',
                     'token.json', 'state.json', 'pending-restore.json'):
        assert excluded not in names
    # The excluded files must also not count towards the file count.
    # (The archive now always carries the sync metadata file too.)
    assert len(names) == 4


def test_archive_excludes_plugin_section_dir(data_dir, tmp_path, work_dir):
    section = data_dir / 'cloudsync'
    section.mkdir()
    (section / 'token.json').write_text('secret')
    (section / 'state.json').write_text('{}')

    archive_path, _metadata = create_library_archive(data_dir, work_dir=work_dir)

    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
    assert not any(name.startswith('cloudsync/') for name in names)


def test_archive_includes_sync_meta(data_dir, work_dir):
    archive_path, _metadata = create_library_archive(data_dir, work_dir=work_dir)

    with zipfile.ZipFile(archive_path) as archive:
        meta = json.loads(archive.read('cloudsync-meta.json'))

    assert meta['format'] == 1
    assert meta['hostname']
    assert meta['created_utc']
    # No merge base exists in the fake data dir: this reads as a
    # never-synced machine, so downloads must treat it additively.
    assert meta['has_base'] is False
    # The fake songs.sqlite has no songs table: no count, no crash.
    assert 'song_count' not in meta


def test_archive_sync_meta_has_base_true(data_dir, work_dir):
    (data_dir / 'cloudsync').mkdir()
    (data_dir / 'cloudsync' / 'base_songs.sqlite').write_bytes(b'fake-base')

    archive_path, _metadata = create_library_archive(data_dir, work_dir=work_dir)

    with zipfile.ZipFile(archive_path) as archive:
        meta = json.loads(archive.read('cloudsync-meta.json'))
    assert meta['has_base'] is True
    # An unreadable base is "unknown", not empty: the downloading side must
    # not treat missing songs as remote deletions.
    assert meta['base_identities'] is None


def test_archive_sync_meta_embeds_base_identities(data_dir, work_dir):
    (data_dir / 'cloudsync').mkdir()
    base_path = data_dir / 'cloudsync' / 'base_songs.sqlite'
    connection = sqlite3.connect(str(base_path))
    connection.executescript('CREATE TABLE songs (id INTEGER PRIMARY KEY, title TEXT, '
                             'search_title TEXT, search_lyrics TEXT, last_modified TEXT, '
                             'temporary INTEGER);\n'
                             "INSERT INTO songs (id, title, search_title) VALUES "
                             "(1, 'Amazing Grace', 'amazing grace'), "
                             "(2, 'Be Thou My Vision', 'be thou my vision')")
    connection.commit()
    connection.close()

    archive_path, _metadata = create_library_archive(data_dir, work_dir=work_dir)

    with zipfile.ZipFile(archive_path) as archive:
        meta = json.loads(archive.read('cloudsync-meta.json'))
    assert meta['has_base'] is True
    assert len(meta['base_identities']) == 2
    assert any('amazing grace' in key for key in meta['base_identities'])


def test_archive_sync_meta_counts_songs(data_dir, work_dir):
    db_path = data_dir / 'songs' / 'songs.sqlite'
    connection = sqlite3.connect(str(db_path))
    connection.execute('CREATE TABLE songs (id INTEGER PRIMARY KEY, title TEXT)')
    connection.executemany('INSERT INTO songs (title) VALUES (?)',
                           [('Amazing Grace',), ('Be Thou My Vision',)])
    connection.commit()
    connection.close()

    archive_path, _metadata = create_library_archive(data_dir, work_dir=work_dir)

    with zipfile.ZipFile(archive_path) as archive:
        meta = json.loads(archive.read('cloudsync-meta.json'))
    assert meta['song_count'] == 2


def test_should_exclude_paths():
    from pathlib import PurePosixPath
    assert _should_exclude(PurePosixPath('cloudsync/token.json'))
    assert _should_exclude(PurePosixPath('cloudsync/state.json'))
    assert _should_exclude(PurePosixPath('sub/dir/debug.log'))
    assert _should_exclude(PurePosixPath('sub/notes.TMP'))
    assert _should_exclude(PurePosixPath('token.json'))
    assert not _should_exclude(PurePosixPath('songs/songs.sqlite'))
    assert not _should_exclude(PurePosixPath('token.json.bak'))


def test_sqlite_database_is_snapshotted(data_dir, tmp_path, work_dir):
    db_path = data_dir / 'songs' / 'songs.sqlite'
    connection = sqlite3.connect(str(db_path))
    connection.execute('CREATE TABLE songs (id INTEGER PRIMARY KEY, title TEXT)')
    connection.execute("INSERT INTO songs (title) VALUES ('Amazing Grace')")
    connection.commit()
    connection.close()

    archive_path, _metadata = create_library_archive(data_dir, work_dir=work_dir)

    extract = tmp_path / 'extract'
    extract.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        archive.extract('songs/songs.sqlite', path=extract)
    connection = sqlite3.connect(str(extract / 'songs' / 'songs.sqlite'))
    rows = connection.execute('SELECT title FROM songs').fetchall()
    connection.close()
    assert rows == [('Amazing Grace',)]


def test_create_archive_missing_dir_raises(tmp_path, work_dir):
    with pytest.raises(FileNotFoundError):
        create_library_archive(tmp_path / 'does-not-exist', work_dir=work_dir)


def test_fingerprint_is_stable(data_dir):
    assert compute_fingerprint(data_dir) == compute_fingerprint(data_dir)


def test_fingerprint_changes_when_file_changes(data_dir):
    before = compute_fingerprint(data_dir)
    target = data_dir / 'notes.txt'
    target.write_text('changed content')
    # Guarantee a distinct mtime even on coarse filesystems.
    stat = target.stat()
    os.utime(target, (stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    assert compute_fingerprint(data_dir) != before


def test_fingerprint_ignores_excluded_files(data_dir):
    before = compute_fingerprint(data_dir)
    (data_dir / 'debug.log').write_text('noise')
    (data_dir / 'cloudsync' / 'token.json').parent.mkdir(exist_ok=True)
    (data_dir / 'cloudsync' / 'token.json').write_text('secret')
    assert compute_fingerprint(data_dir) == before
