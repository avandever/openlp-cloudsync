"""
Song-level merge for the Cloud Sync plugin.

When two machines have both changed their libraries since the last sync,
whole-library replacement would silently drop one side's work.  This module
merges at the song level instead, using only the standard library
(``sqlite3``).

Two modes, selected by the ``base_db_path`` argument of
:func:`merge_songs_databases`:

* **Three-way** (base given) -- the base is a snapshot of ``songs.sqlite``
  taken at the last successful sync (see :func:`save_merge_base`).  A song
  present in the base but missing on one side was deleted there: if the
  other side left it untouched the deletion propagates; if the other side
  edited it, the edit wins -- data is never silently lost -- and the
  conflict is reported.
* **Two-way** (no base) -- the original behavior, used when there is no
  common ancestor yet (e.g. the first sync after upgrading).  New remote
  songs are unioned in and same-song edits resolve by ``last_modified``.
  Nothing is ever deleted in this mode.

Identity, relations and temporary-song handling are the same in both modes:

* **Identity** -- a song is identified by its normalized title plus its
  authors (``(search_title, sorted author display names)``).  There is no
  global song UUID in OpenLP's schema, so a renamed title or changed author
  list looks like a delete plus an add; that edge is logged, not resolved.
* **New songs** -- songs present on the remote side but not locally are
  inserted with fresh ids, along with their authors, songbook entries,
  topics and media-file rows (matched-or-created by name so nothing
  duplicates).
* **Edited on both sides** -- the same identity with different lyrics is
  resolved per song by ``last_modified``: newer wins, keeping the local id.
  Its relations (authors, songbooks, topics, media files) are re-synced
  from the winning side.
* **Temporary songs** (OpenLP's ephemeral scratch songs) are never
  imported.

Only ``songs.sqlite`` is merged.  Bibles, themes, custom slides and media
keep last-write-wins semantics at the whole-library level.
"""
import logging
import os
import sqlite3
from pathlib import Path

from .backup import BASE_SONGS_FILENAME, PLUGIN_SECTION_DIR

log = logging.getLogger(__name__)

# File name of the last-synced songs.sqlite snapshot inside the plugin's
# section directory (defined in backup.py; imported above).  The section
# directory is excluded from both the sync fingerprint and the uploaded
# archives, so the base never perturbs sync decisions.

# Columns copied when a song row moves between databases ('id' is always
# re-allocated on the local side).
SONG_COPY_COLUMNS = [
    'title', 'alternate_title', 'lyrics', 'verse_order', 'copyright',
    'comments', 'ccli_number', 'theme_name', 'search_title', 'search_lyrics',
    'create_date', 'last_modified', 'temporary',
]

# Columns copied when an author row is created locally.
AUTHOR_COPY_COLUMNS = ['first_name', 'last_name', 'display_name']


def merge_base_path(data_dir):
    """
    Path of the last-synced ``songs.sqlite`` snapshot used as the common
    ancestor for three-way merges.

    :param data_dir: The OpenLP data directory.
    """
    return Path(data_dir) / PLUGIN_SECTION_DIR / BASE_SONGS_FILENAME


def find_songs_db(root):
    """
    Locate the songs database under ``root``.

    OpenLP keeps it at ``<data>/songs/songs.sqlite`` (the songs plugin's
    section directory); some layouts may have it directly at
    ``<root>/songs.sqlite``.  Returns the first existing candidate, or
    None when neither exists.
    """
    candidates = (Path(root) / 'songs' / 'songs.sqlite',
                  Path(root) / 'songs.sqlite')
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def save_merge_base(data_dir, songs_db_path=None):
    """
    Snapshot ``songs.sqlite`` as the merge base for future three-way
    merges.  Call after every sync operation that leaves the local library
    in agreement with the cloud (upload, merge-and-upload, applied
    download).  The base lives in the plugin's section directory, which is
    excluded from the sync fingerprint and from uploaded archives.

    :param data_dir: The OpenLP data directory.
    :param songs_db_path: Path to ``songs.sqlite``; defaults to the live
        database found under ``data_dir``.
    :return: True if the base was saved, False otherwise.
    """
    songs_db_path = Path(songs_db_path) if songs_db_path else find_songs_db(data_dir)
    if songs_db_path is None or not songs_db_path.is_file():
        log.warning('Cannot save merge base: no songs database found under %s', data_dir)
        return False
    base_path = merge_base_path(data_dir)
    base_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = base_path.with_name(base_path.name + '.tmp')
    try:
        source = sqlite3.connect(str(songs_db_path), timeout=30.0)
        try:
            target = sqlite3.connect(str(tmp_path), timeout=30.0)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        os.replace(str(tmp_path), str(base_path))
    except sqlite3.Error:
        log.exception('Could not snapshot songs database for merge base')
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    log.info('Saved merge base to %s', base_path)
    return True


def _table_exists(connection, name):
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return row is not None


def _rows(connection, query, params=()):
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, params)]
    finally:
        connection.row_factory = None


def _next_id(connection, table):
    row = connection.execute('SELECT COALESCE(MAX(id), 0) FROM {table}'.format(table=table)).fetchone()
    return (row[0] or 0) + 1


def _song_identity(song, authors):
    """
    The merge identity of a song: normalized title plus sorted author names.
    """
    names = tuple(sorted(author['display_name'] for author in authors))
    return (song.get('search_title') or '', names)


def _authors_for_song(connection, song_id):
    return _rows(connection,
                 'SELECT a.id, a.first_name, a.last_name, a.display_name '
                 'FROM authors a JOIN authors_songs aus ON aus.author_id = a.id '
                 'WHERE aus.song_id = ? ORDER BY a.display_name', (song_id,))


def _find_or_create_author(local, remote_author):
    existing = local.execute('SELECT id FROM authors WHERE display_name = ?',
                             (remote_author['display_name'],)).fetchone()
    if existing:
        return existing[0]
    author_id = _next_id(local, 'authors')
    local.execute(
        'INSERT INTO authors (id, first_name, last_name, display_name) VALUES (?, ?, ?, ?)',
        (author_id, remote_author.get('first_name'), remote_author.get('last_name'),
         remote_author['display_name']))
    return author_id


def _find_or_create_songbook(local, remote, book_id):
    book = _rows(remote, 'SELECT id, name, publisher FROM song_books WHERE id = ?', (book_id,))
    if not book:
        return None
    book = book[0]
    existing = local.execute('SELECT id FROM song_books WHERE name = ?', (book['name'],)).fetchone()
    if existing:
        return existing[0]
    new_id = _next_id(local, 'song_books')
    local.execute('INSERT INTO song_books (id, name, publisher) VALUES (?, ?, ?)',
                  (new_id, book['name'], book.get('publisher')))
    return new_id


def _find_or_create_topic(local, remote, topic_id):
    topic = _rows(remote, 'SELECT id, name FROM topics WHERE id = ?', (topic_id,))
    if not topic:
        return None
    name = topic[0]['name']
    existing = local.execute('SELECT id FROM topics WHERE name = ?', (name,)).fetchone()
    if existing:
        return existing[0]
    new_id = _next_id(local, 'topics')
    local.execute('INSERT INTO topics (id, name) VALUES (?, ?)', (new_id, name))
    return new_id


def _copy_relations(local, remote, remote_song_id, local_song_id, replace=False):
    """
    Copy a song's authors, songbook entries, topics and media files from the
    remote database to the local one.  With ``replace=True`` the local links
    are cleared first (used when the remote side won a per-song conflict).
    """
    if replace:
        local.execute('DELETE FROM authors_songs WHERE song_id = ?', (local_song_id,))
        local.execute('DELETE FROM songs_songbooks WHERE song_id = ?', (local_song_id,))
        local.execute('DELETE FROM songs_topics WHERE song_id = ?', (local_song_id,))
        local.execute('DELETE FROM media_files WHERE song_id = ?', (local_song_id,))
    # Authors.
    for link in _rows(remote, 'SELECT author_id, author_type FROM authors_songs WHERE song_id = ?',
                      (remote_song_id,)):
        authors = _rows(remote, 'SELECT first_name, last_name, display_name FROM authors WHERE id = ?',
                        (link['author_id'],))
        if not authors:
            continue
        author_id = _find_or_create_author(local, authors[0])
        local.execute('INSERT OR IGNORE INTO authors_songs (author_id, song_id, author_type) '
                      'VALUES (?, ?, ?)', (author_id, local_song_id, link.get('author_type') or ''))
    # Songbook entries.
    for entry in _rows(remote, 'SELECT songbook_id, entry FROM songs_songbooks WHERE song_id = ?',
                       (remote_song_id,)):
        book_id = _find_or_create_songbook(local, remote, entry['songbook_id'])
        if book_id is None:
            continue
        local.execute('INSERT OR IGNORE INTO songs_songbooks (songbook_id, song_id, entry) '
                      'VALUES (?, ?, ?)', (book_id, local_song_id, entry['entry']))
    # Topics.
    for link in _rows(remote, 'SELECT topic_id FROM songs_topics WHERE song_id = ?', (remote_song_id,)):
        topic_id = _find_or_create_topic(local, remote, link['topic_id'])
        if topic_id is None:
            continue
        local.execute('INSERT OR IGNORE INTO songs_topics (song_id, topic_id) VALUES (?, ?)',
                      (local_song_id, topic_id))
    # Media files (rows only; the audio files themselves live outside the
    # data dir and do not transfer -- same as a whole-library restore).
    for media in _rows(remote, 'SELECT file_path, file_hash, type, weight FROM media_files '
                               'WHERE song_id = ? ORDER BY weight', (remote_song_id,)):
        media_id = _next_id(local, 'media_files')
        local.execute('INSERT INTO media_files (id, song_id, file_path, file_hash, type, weight) '
                      'VALUES (?, ?, ?, ?, ?, ?)',
                      (media_id, local_song_id, media['file_path'], media.get('file_hash'),
                       media.get('type') or 'audio', media.get('weight') or 0))


def _insert_song(local, remote_song):
    song_id = _next_id(local, 'songs')
    columns = [column for column in SONG_COPY_COLUMNS]
    placeholders = ', '.join('?' for _ in columns)
    local.execute('INSERT INTO songs (id, {columns}) VALUES (?, {placeholders})'.format(
        columns=', '.join(columns), placeholders=placeholders),
        [song_id] + [remote_song.get(column) for column in columns])
    return song_id


def _update_song(local, local_song_id, remote_song):
    # Keep the local id and create_date; take everything else from the
    # winning (remote) side, including its last_modified.
    columns = [column for column in SONG_COPY_COLUMNS if column not in ('create_date',)]
    assignments = ', '.join('{column} = ?'.format(column=column) for column in columns)
    local.execute('UPDATE songs SET {assignments} WHERE id = ?'.format(assignments=assignments),
                  [remote_song.get(column) for column in columns] + [local_song_id])


def _delete_song(local, local_song_id):
    """
    Delete a song and its relation rows (author links, songbook entries,
    topic links, media-file rows).  Shared rows (authors, song books,
    topics themselves) are left alone -- other songs may use them.
    """
    local.execute('DELETE FROM authors_songs WHERE song_id = ?', (local_song_id,))
    local.execute('DELETE FROM songs_songbooks WHERE song_id = ?', (local_song_id,))
    local.execute('DELETE FROM songs_topics WHERE song_id = ?', (local_song_id,))
    local.execute('DELETE FROM media_files WHERE song_id = ?', (local_song_id,))
    local.execute('DELETE FROM songs WHERE id = ?', (local_song_id,))


def _identity_map(connection, skip_temporary):
    """
    Map song identities to song rows for one database.
    """
    songs = {}
    for song in _rows(connection, 'SELECT * FROM songs'):
        if skip_temporary and song.get('temporary'):
            continue
        authors = _authors_for_song(connection, song['id'])
        songs[_song_identity(song, authors)] = song
    return songs


def _identity_key_string(key):
    """
    Serialize a merge-identity key to a string for embedding in sync
    metadata (JSON-safe and deterministic across machines).
    """
    return '\x1f'.join([key[0]] + list(key[1]))


def base_identity_keys(base_db_path):
    """
    Read a merge-base snapshot and return its song identities as a sorted
    list of serialized key strings (see :func:`_identity_key_string`).

    A backup embeds these so a downloading machine can tell a genuine
    remote deletion (the song was in the uploader's base, now gone) from a
    song the uploader never had.

    :return: Sorted list of identity key strings; empty when the base has
        no songs table or no songs.
    """
    base = sqlite3.connect('file:{path}?mode=ro'.format(path=str(base_db_path)), uri=True,
                           timeout=30.0)
    try:
        if not _table_exists(base, 'songs'):
            return []
        if _table_exists(base, 'authors') and _table_exists(base, 'authors_songs'):
            identities = _identity_map(base, skip_temporary=True)
        else:
            # A base snapshot without author tables: identity falls back to
            # title-only keys.  Still deterministic for both sides.
            identities = {}
            for song in _rows(base, 'SELECT * FROM songs'):
                if song.get('temporary'):
                    continue
                identities[_song_identity(song, [])] = song
        return sorted(_identity_key_string(key) for key in identities)
    finally:
        base.close()


def _lyrics_of(song):
    return song.get('search_lyrics') or ''


def _modified_of(song):
    return song.get('last_modified') or ''


def _display_title(song):
    return song.get('title') or song.get('search_title') or '?'


def merge_songs_databases(local_db_path, remote_db_path, base_db_path=None,
                          remote_base_identities=None):
    """
    Merge songs from ``remote_db_path`` into ``local_db_path`` (both paths
    to ``songs.sqlite`` files).

    :param base_db_path: Optional path to the merge-base snapshot (see
        :func:`save_merge_base`).  When given and present, the merge is
        three-way; otherwise it is two-way and never deletes.
    :param remote_base_identities: The set of identity key strings the
        remote backup's uploader had at its own last sync (from the
        backup's sync metadata; see :func:`base_identity_keys`), or None
        when unknown.  A song missing from the remote backup counts as
        deleted on the remote side only when it was in the uploader's base:
        otherwise the uploader simply never had it, and the local song is
        kept.  Defaults to None (safe) so backups without sync metadata
        never delete.
    :return: A report dict with ``added``, ``updated``, ``deleted`` and
        ``kept_local`` counts plus a ``details`` list of human-readable
        lines.
    """
    report = {'added': 0, 'updated': 0, 'deleted': 0, 'kept_local': 0, 'details': []}
    local = sqlite3.connect(str(local_db_path), timeout=30.0)
    remote = sqlite3.connect('file:{path}?mode=ro'.format(path=str(remote_db_path)), uri=True,
                             timeout=30.0)
    base = None
    try:
        if not _table_exists(local, 'songs') or not _table_exists(remote, 'songs'):
            raise ValueError('songs table missing from one of the databases')
        local_songs = _identity_map(local, skip_temporary=False)
        remote_songs = _identity_map(remote, skip_temporary=True)
        base_songs = {}
        if base_db_path and Path(base_db_path).is_file():
            base = sqlite3.connect('file:{path}?mode=ro'.format(path=str(base_db_path)), uri=True,
                                   timeout=30.0)
            if _table_exists(base, 'songs'):
                base_songs = _identity_map(base, skip_temporary=True)
            else:
                log.warning('Merge base %s has no songs table; merging two-way', base_db_path)
        three_way = bool(base_songs) or (base is not None and _table_exists(base, 'songs'))
        log.info('Merging %d remote songs into %d local songs%s', len(remote_songs), len(local_songs),
                 ' (three-way)' if three_way else ' (two-way)')
        for key in sorted(set(local_songs) | set(remote_songs) | set(base_songs),
                          key=lambda key: (key[0], ' '.join(key[1]))):
            local_song = local_songs.get(key)
            remote_song = remote_songs.get(key)
            base_song = base_songs.get(key)
            title = _display_title(remote_song or local_song or base_song)
            if base_song is not None:
                _merge_with_base(local, remote, local_song, remote_song, base_song, report, title,
                                 _identity_key_string(key), remote_base_identities)
            elif remote_song is not None and local_song is None:
                # Added on the remote side (or no base to compare against).
                new_id = _insert_song(local, remote_song)
                _copy_relations(local, remote, remote_song['id'], new_id)
                local_songs[key] = dict(remote_song, id=new_id)
                report['added'] += 1
                report['details'].append('Added "{title}"'.format(title=title))
            elif remote_song is not None and _lyrics_of(remote_song) != _lyrics_of(local_song):
                # Added independently on both sides, or edited on both
                # sides with no base: newer last_modified wins.
                local_modified = _modified_of(local_song)
                if _modified_of(remote_song) > local_modified:
                    _update_song(local, local_song['id'], remote_song)
                    _copy_relations(local, remote, remote_song['id'], local_song['id'], replace=True)
                    report['updated'] += 1
                    report['details'].append('Updated "{title}" (newer on remote)'.format(title=title))
                else:
                    report['kept_local'] += 1
                    report['details'].append('Kept local "{title}" (newer locally)'.format(title=title))
            # Local-only songs are always kept.
        local.commit()
    finally:
        local.close()
        remote.close()
        if base is not None:
            base.close()
    log.info('Song merge complete: %d added, %d updated, %d deleted, %d kept local',
             report['added'], report['updated'], report['deleted'], report['kept_local'])
    return report


def _merge_with_base(local, remote, local_song, remote_song, base_song, report, title,
                     identity_key, remote_base_identities=None):
    """
    Three-way merge for one song identity present in the base snapshot.

    :param identity_key: Serialized identity key of this song.
    :param remote_base_identities: The set of identity key strings the
        remote backup's uploader had at its own last sync, or None when
        unknown.  A song missing from the remote backup is a deletion on
        the remote side only when the uploader had it in its base;
        otherwise the uploader never had it and the local song is kept.
    """
    if local_song is not None and remote_song is not None:
        if _lyrics_of(remote_song) != _lyrics_of(local_song):
            local_modified = _modified_of(local_song)
            if _modified_of(remote_song) > local_modified:
                _update_song(local, local_song['id'], remote_song)
                _copy_relations(local, remote, remote_song['id'], local_song['id'], replace=True)
                report['updated'] += 1
                report['details'].append('Updated "{title}" (newer on remote)'.format(title=title))
            else:
                report['kept_local'] += 1
                report['details'].append('Kept local "{title}" (newer locally)'.format(title=title))
        return
    if local_song is not None:
        # Missing on the remote side.
        deleted_on_remote = (remote_base_identities is not None
                             and identity_key in remote_base_identities)
        if deleted_on_remote and _lyrics_of(local_song) == _lyrics_of(base_song):
            # The remote side had this song at its last sync and it is gone
            # now: a deliberate deletion there.
            _delete_song(local, local_song['id'])
            report['deleted'] += 1
            report['details'].append('Deleted "{title}" (deleted on remote)'.format(title=title))
        elif deleted_on_remote:
            # Edited locally but deleted on the remote side: the edit wins
            # so no work is silently lost.
            report['kept_local'] += 1
            report['details'].append(
                'Kept local "{title}" (edited locally, deleted on remote)'.format(title=title))
        else:
            # The remote backup's uploader never had this song (or its
            # history is unknown): its absence is not a deletion.
            report['kept_local'] += 1
            report['details'].append(
                'Kept local "{title}" (not in remote backup)'.format(title=title))
        return
    if remote_song is not None:
        # Missing locally: deleted here.
        if _lyrics_of(remote_song) == _lyrics_of(base_song):
            # Untouched on the remote side: stay deleted, do not resurrect.
            report['details'].append('Not re-adding "{title}" (deleted locally)'.format(title=title))
        else:
            # Edited on the remote side but deleted locally: take the edit
            # so no work is silently lost.
            new_id = _insert_song(local, remote_song)
            _copy_relations(local, remote, remote_song['id'], new_id)
            report['added'] += 1
            report['details'].append(
                'Re-added "{title}" (edited on remote, deleted locally)'.format(title=title))
        return
    # Missing on both sides: deleted everywhere, nothing to do.
