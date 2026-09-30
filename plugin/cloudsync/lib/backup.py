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
# You should have received a copy of the GNU General Public License      #
# along with this program.  If not, see <https://www.gnu.org/licenses/>. #
##########################################################################
"""
The :mod:`~openlp.plugins.cloudsync.lib.backup` module creates consistent,
self-describing snapshots of the OpenLP data directory for cloud sync.

OpenLP keeps its SQLite databases open while it runs, so copying the raw
files can produce a corrupt backup.  Every ``*.sqlite``/``*.db`` file is
therefore snapshotted with SQLite's ``VACUUM INTO`` (a transactionally
consistent copy) before being zipped.  The plugin's own working files
(tokens, sync state, staged downloads) are excluded from the archive.
"""
import hashlib
import json
import logging
import shutil
import socket
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Sub-directory (relative to the OpenLP data dir) holding this plugin's own
# working files: OAuth token, sync state, staged downloads, local backups.
# It is never included in an uploaded archive (it holds machine-specific
# credentials and would otherwise recurse).
PLUGIN_SECTION_DIR = 'cloudsync'

# File name of the merge-base snapshot inside the plugin section directory.
BASE_SONGS_FILENAME = 'base_songs.sqlite'

# Name of the JSON metadata file written into every archive describing the
# backup for sync purposes (whether the uploader had a merge base, song
# count, ...).  Never copied back into the data directory on download.
CLOUDSYNC_META_FILENAME = 'cloudsync-meta.json'

# File suffixes treated as live SQLite databases needing VACUUM INTO.
SQLITE_SUFFIXES = {'.sqlite', '.db'}

# File name patterns excluded from archives (locks, logs, temp files).
EXCLUDED_SUFFIXES = {'.log', '.tmp', '.lock', '.swp'}
EXCLUDED_NAMES = {'token.json', 'state.json', 'pending-restore.json'}


def compute_sha256(path):
    """
    Compute the SHA-256 hex digest of a file.

    :param path: Path to the file.
    :return: Hex digest string.
    """
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_sqlite(source_path, staging_dir):
    """
    Create a transactionally consistent copy of a (possibly open) SQLite
    database using ``VACUUM INTO``.

    :param source_path: Path to the live database file.
    :param staging_dir: Directory to write the snapshot into.
    :return: Path of the snapshot file.
    """
    snapshot_path = staging_dir / source_path.name
    log.debug('Snapshotting live database %s', source_path)
    connection = sqlite3.connect('file:{path}?mode=ro'.format(path=source_path), uri=True, timeout=30.0)
    try:
        # VACUUM INTO writes a consistent copy even while other connections
        # hold the database open.
        connection.execute("VACUUM INTO '{path}'".format(path=str(snapshot_path).replace("'", "''")))
    finally:
        connection.close()
    return snapshot_path


def _should_exclude(relative_path):
    """
    Decide whether a data-dir-relative path must be left out of the archive.

    :param relative_path: Path relative to the OpenLP data directory.
    :return: True if the file must be excluded.
    """
    parts = relative_path.parts
    if parts and parts[0] == PLUGIN_SECTION_DIR:
        return True
    name = relative_path.name
    if name in EXCLUDED_NAMES:
        return True
    if relative_path.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    return False


def create_library_archive(data_dir, work_dir=None):
    """
    Build a zip archive of the OpenLP data directory with consistent
    database snapshots.

    :param data_dir: The OpenLP data directory to archive.
    :param work_dir: Directory for the staging area and resulting zip.
        Defaults to a fresh temporary directory.
    :return: Tuple of (archive path, metadata dict).
    """
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise FileNotFoundError('OpenLP data directory not found: {path}'.format(path=data_dir))
    hostname = socket.gethostname().replace(' ', '_')
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    archive_name = 'openlp-library-{host}-{ts}.zip'.format(host=hostname, ts=timestamp)
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix='openlp-cloudsync-'))
    else:
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = work_dir / 'staging'
    staging_dir.mkdir(parents=True, exist_ok=True)
    archive_path = work_dir / archive_name

    log.info('Creating library archive from %s', data_dir)
    file_count = 0
    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source_path in sorted(data_dir.rglob('*')):
            if not source_path.is_file():
                continue
            relative_path = source_path.relative_to(data_dir)
            if _should_exclude(relative_path):
                log.debug('Excluding %s from archive', relative_path)
                continue
            if source_path.suffix.lower() in SQLITE_SUFFIXES:
                try:
                    snapshot = _snapshot_sqlite(source_path, staging_dir)
                except sqlite3.Error:
                    log.exception('Could not snapshot database %s, copying raw file instead', source_path)
                    archive.write(source_path, str(relative_path))
                else:
                    archive.write(snapshot, str(relative_path))
            else:
                archive.write(source_path, str(relative_path))
            file_count += 1
        # Sync metadata: lets the downloading side merge safely.  The backup
        # records which songs the uploader had at its last sync, so a song
        # missing from the backup only reads as "deleted on the remote
        # side" when the uploader actually had it -- never just because
        # the uploader's library is smaller.
        base_songs_path = data_dir / PLUGIN_SECTION_DIR / BASE_SONGS_FILENAME
        sync_meta = {
            'format': 1,
            'hostname': hostname,
            'created_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'has_base': base_songs_path.is_file(),
            'base_identities': _read_base_identities(base_songs_path),
        }
        song_count = _count_staged_songs(staging_dir)
        if song_count is not None:
            sync_meta['song_count'] = song_count
        archive.writestr(CLOUDSYNC_META_FILENAME, json.dumps(sync_meta, indent=2))
    sha256 = compute_sha256(archive_path)
    metadata = {
        'name': archive_name,
        'hostname': hostname,
        'created_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'sha256': sha256,
        'size_bytes': archive_path.stat().st_size,
        'file_count': file_count,
    }
    try:
        import openlp
        metadata['openlp_version'] = getattr(openlp, '__version__', 'unknown')
    except Exception:
        metadata['openlp_version'] = 'unknown'
    log.info('Archive %s created: %d files, %d bytes, sha256=%s',
             archive_name, file_count, metadata['size_bytes'], sha256[:12])
    shutil.rmtree(staging_dir, ignore_errors=True)
    return archive_path, metadata


def _read_base_identities(base_songs_path):
    """
    Best-effort read of the merge-base snapshot's song identities for the
    sync metadata (see :func:`songmerge.base_identity_keys`).

    :return: Sorted list of identity key strings, or None when there is no
        base snapshot or it cannot be read.  None means "unknown" to the
        downloading side, which then never treats a missing song as a
        remote deletion.
    """
    if not Path(base_songs_path).is_file():
        return None
    try:
        # Deferred import: songmerge imports names from this module.
        from .songmerge import base_identity_keys
        return base_identity_keys(base_songs_path)
    except Exception:
        log.exception('Could not read base identities for sync metadata')
        return None


def _count_staged_songs(staging_dir):
    """
    Best-effort count of songs in the staged ``songs.sqlite`` snapshot.

    :param staging_dir: The staging directory used while building the archive.
    :return: Song count, or None when it cannot be determined.
    """
    snapshot = Path(staging_dir) / 'songs.sqlite'
    if not snapshot.is_file():
        return None
    try:
        connection = sqlite3.connect('file:{path}?mode=ro'.format(path=snapshot), uri=True,
                                     timeout=10.0)
        try:
            return connection.execute('SELECT COUNT(*) FROM songs').fetchone()[0]
        finally:
            connection.close()
    except sqlite3.Error:
        return None


def compute_fingerprint(data_dir):
    """
    Compute a cheap fingerprint of the data directory used to detect local
    changes without re-hashing every file's contents.

    The fingerprint covers each file's relative path, size and mtime, which
    is enough to notice any song/database/media change made by OpenLP.

    :param data_dir: The OpenLP data directory.
    :return: Hex digest string.
    """
    data_dir = Path(data_dir)
    digest = hashlib.sha256()
    entries = []
    for source_path in sorted(data_dir.rglob('*')):
        if not source_path.is_file():
            continue
        relative_path = source_path.relative_to(data_dir)
        if _should_exclude(relative_path):
            continue
        try:
            stat = source_path.stat()
        except OSError:
            continue
        entries.append('{path}\0{size}\0{mtime}'.format(
            path=str(relative_path), size=stat.st_size, mtime=stat.st_mtime_ns))
    digest.update('\n'.join(entries).encode('utf-8'))
    return digest.hexdigest()
