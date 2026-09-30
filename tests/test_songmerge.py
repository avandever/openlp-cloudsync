"""
Tests for the song-level merge engine (:mod:`songmerge`).

Small ``songs.sqlite`` files are built directly with ``sqlite3`` -- no
SQLAlchemy, no OpenLP stack -- matching the real 3.1.3 schema closely
enough for the merge queries.
"""
import ast
import re
import sqlite3
from pathlib import Path

import pytest

from openlp.plugins.cloudsync.lib import songmerge
from openlp.plugins.cloudsync.lib.songmerge import merge_songs_databases


# The real table names in OpenLP 3.1.3's songs database, taken from
# openlp/plugins/songs/lib/db.py (__tablename__ attributes).  The merge
# engine's SQL must only touch these (plus sqlite_master for introspection).
REAL_SONG_TABLES = {
    'songs', 'authors', 'authors_songs', 'song_books', 'songs_songbooks',
    'topics', 'songs_topics', 'media_files', 'sqlite_master',
}


def _tables_referenced_in_merge_sql():
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    source = repo_root / 'openlp' / 'plugins' / 'cloudsync' / 'lib' / 'songmerge.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    # Only inspect real string literals (SQL lives in strings); this keeps
    # comments and prose out of the table-name hunt.  Only literals that
    # start with a SQL verb are statements -- docstrings and error messages
    # are skipped.
    strings = [node.value for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and re.match(r'\s*(SELECT|INSERT|UPDATE|DELETE)\b', node.value, re.IGNORECASE)]
    tables = set()
    for text in strings:
        for pattern in (r'\bFROM\s+([a-z_][a-z0-9_]*)', r'\bINTO\s+([a-z_][a-z0-9_]*)',
                        r'\bUPDATE\s+([a-z_][a-z0-9_]*)', r'\bTABLE\s+([a-z_][a-z0-9_]*)'):
            tables.update(match.group(1) for match in re.finditer(pattern, text, re.IGNORECASE))
    return tables


def test_merge_sql_only_uses_real_openlp_tables():
    """
    Regression test: the merge engine once referenced a ``songbook_entries``
    table that does not exist in OpenLP's real schema (it is
    ``songs_songbooks``), which crashed every live merge on real libraries
    while the tests -- built on a hand-written schema -- stayed green.
    """
    unknown = _tables_referenced_in_merge_sql() - REAL_SONG_TABLES
    assert not unknown, 'merge SQL references tables missing from the real OpenLP schema: %s' % sorted(unknown)

SCHEMA = """
CREATE TABLE songs (id INTEGER PRIMARY KEY, title TEXT NOT NULL, alternate_title TEXT,
    lyrics TEXT NOT NULL, verse_order TEXT, copyright TEXT, comments TEXT, ccli_number TEXT,
    theme_name TEXT, search_title TEXT NOT NULL, search_lyrics TEXT NOT NULL,
    create_date TEXT, last_modified TEXT, temporary INTEGER DEFAULT 0);
CREATE TABLE authors (id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, display_name TEXT NOT NULL);
CREATE TABLE authors_songs (author_id INTEGER NOT NULL, song_id INTEGER NOT NULL, author_type TEXT NOT NULL DEFAULT '');
CREATE TABLE song_books (id INTEGER PRIMARY KEY, name TEXT NOT NULL, publisher TEXT);
CREATE TABLE songs_songbooks (songbook_id INTEGER NOT NULL, song_id INTEGER NOT NULL, entry TEXT NOT NULL);
CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE songs_topics (song_id INTEGER NOT NULL, topic_id INTEGER NOT NULL);
CREATE TABLE media_files (id INTEGER PRIMARY KEY, song_id INTEGER, file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL, type TEXT DEFAULT 'audio', weight INTEGER DEFAULT 0);
"""


def make_db(path, songs=()):
    connection = sqlite3.connect(str(path))
    connection.executescript(SCHEMA)
    for song in songs:
        authors = song.pop('authors', [])
        connection.execute(
            'INSERT INTO songs (id, title, lyrics, search_title, search_lyrics, '
            'last_modified, temporary) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (song['id'], song['title'], song.get('lyrics', 'la la'),
             song['title'].lower(), song.get('lyrics', 'la la').lower(),
             song.get('last_modified', '2026-01-01 00:00:00'), int(song.get('temporary', False))))
        for author in authors:
            row = connection.execute('SELECT id FROM authors WHERE display_name = ?',
                                     (author,)).fetchone()
            if row is None:
                author_id = (connection.execute('SELECT COALESCE(MAX(id), 0) FROM authors')
                             .fetchone()[0] or 0) + 1
                connection.execute('INSERT INTO authors (id, display_name) VALUES (?, ?)',
                                   (author_id, author))
            else:
                author_id = row[0]
            connection.execute('INSERT INTO authors_songs (author_id, song_id, author_type) '
                               'VALUES (?, ?, ?)', (author_id, song['id'], 'words'))
    connection.commit()
    connection.close()


def song_titles(path):
    connection = sqlite3.connect(str(path))
    titles = [row[0] for row in connection.execute('SELECT title FROM songs ORDER BY id')]
    connection.close()
    return titles


def test_new_remote_songs_are_added(tmp_path):
    local_db = tmp_path / 'local.sqlite'
    remote_db = tmp_path / 'remote.sqlite'
    make_db(local_db, [{'id': 1, 'title': 'Amazing Grace', 'authors': ['John Newton']}])
    make_db(remote_db, [{'id': 1, 'title': 'Amazing Grace', 'authors': ['John Newton']},
                        {'id': 2, 'title': 'Be Thou My Vision', 'authors': ['Mary Byrne']}])

    report = merge_songs_databases(local_db, remote_db)

    assert report['added'] == 1
    assert song_titles(local_db) == ['Amazing Grace', 'Be Thou My Vision']
    connection = sqlite3.connect(str(local_db))
    try:
        assert connection.execute('SELECT COUNT(*) FROM authors').fetchone()[0] == 2
        assert connection.execute('SELECT COUNT(*) FROM authors_songs').fetchone()[0] == 2
    finally:
        connection.close()


def test_temporary_songs_are_skipped(tmp_path):
    local_db = tmp_path / 'local.sqlite'
    remote_db = tmp_path / 'remote.sqlite'
    make_db(local_db)
    make_db(remote_db, [{'id': 5, 'title': 'Scratch', 'temporary': True}])

    report = merge_songs_databases(local_db, remote_db)

    assert report['added'] == 0
    assert song_titles(local_db) == []


def test_newer_remote_lyrics_win(tmp_path):
    local_db = tmp_path / 'local.sqlite'
    remote_db = tmp_path / 'remote.sqlite'
    make_db(local_db, [{'id': 1, 'title': 'Amazing Grace', 'authors': ['John Newton'],
                        'lyrics': 'old words', 'last_modified': '2026-01-01 00:00:00'}])
    make_db(remote_db, [{'id': 9, 'title': 'Amazing Grace', 'authors': ['John Newton'],
                         'lyrics': 'new words', 'last_modified': '2026-09-01 00:00:00'}])

    report = merge_songs_databases(local_db, remote_db)

    assert report['updated'] == 1
    connection = sqlite3.connect(str(local_db))
    try:
        row = connection.execute('SELECT id, lyrics FROM songs').fetchone()
        assert row[0] == 1  # local id kept
        assert row[1] == 'new words'
    finally:
        connection.close()


def test_older_remote_lyrics_keep_local(tmp_path):
    local_db = tmp_path / 'local.sqlite'
    remote_db = tmp_path / 'remote.sqlite'
    make_db(local_db, [{'id': 1, 'title': 'Amazing Grace', 'authors': ['John Newton'],
                        'lyrics': 'local words', 'last_modified': '2026-09-01 00:00:00'}])
    make_db(remote_db, [{'id': 9, 'title': 'Amazing Grace', 'authors': ['John Newton'],
                         'lyrics': 'stale words', 'last_modified': '2026-01-01 00:00:00'}])

    report = merge_songs_databases(local_db, remote_db)

    assert report['updated'] == 0
    assert report['kept_local'] == 1
    connection = sqlite3.connect(str(local_db))
    try:
        assert connection.execute('SELECT lyrics FROM songs').fetchone()[0] == 'local words'
    finally:
        connection.close()


def test_authors_matched_by_display_name(tmp_path):
    local_db = tmp_path / 'local.sqlite'
    remote_db = tmp_path / 'remote.sqlite'
    make_db(local_db, [{'id': 1, 'title': 'Old Song', 'authors': ['John Newton']}])
    make_db(remote_db, [{'id': 7, 'title': 'New Song', 'authors': ['John Newton']}])

    merge_songs_databases(local_db, remote_db)

    connection = sqlite3.connect(str(local_db))
    try:
        assert connection.execute("SELECT COUNT(*) FROM authors WHERE display_name = 'John Newton'"
                                  ).fetchone()[0] == 1
    finally:
        connection.close()


def test_missing_songs_table_raises(tmp_path):
    local_db = tmp_path / 'local.sqlite'
    remote_db = tmp_path / 'remote.sqlite'
    sqlite3.connect(str(local_db)).close()
    sqlite3.connect(str(remote_db)).close()

    with pytest.raises(ValueError):
        merge_songs_databases(local_db, remote_db)
