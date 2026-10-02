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
The :mod:`~..synccontroller` module orchestrates
automatic library synchronisation.

Two triggers feed into the controller:

* **Startup** (:meth:`SyncController.startup_sync`, called from the plugin's
  ``app_startup`` hook): compares the local library fingerprint and the
  newest remote backup against the persisted sync state, then uploads,
  downloads (applied live: songs are merged into the running library and
  the song list refreshes in place), or does nothing.
* **Song changes** (:meth:`SyncController.on_song_changed`, wired to the
  ``song_changed`` registry event fired by the song editor): debounced, then
  uploads the library.

All network and disk-heavy work runs in
:class:`~..worker.SyncWorker` threads; the
controller itself lives on the GUI thread.
"""
import logging
import inspect
import shutil
import socket
import tempfile
import zipfile
from collections import namedtuple
from pathlib import Path

from .qtcompat import QtCore

from openlp.core.common.applocation import AppLocation
from openlp.core.common.i18n import translate
from openlp.core.common.registry import Registry
from openlp.core.threading import is_thread_finished
from openlp.core.threading import run_thread as _openlp_run_thread
from . import auth as auth_module
from .backup import CLOUDSYNC_META_FILENAME, compute_fingerprint, compute_sha256, create_library_archive
from .providers import RemoteBackup, get_provider
from .restore import discard_pending_restore, has_pending_restore
from .songmerge import find_songs_db
from .songrefresh import refresh_songs_ui
from .state import load_state, record_remote_seen, record_upload

log = logging.getLogger(__name__)

# OpenLP 3.1.x's run_thread() has no ``queued_connections`` argument (it was
# added with the PySide6 migration); on those versions connect the signals
# directly instead.  PyQt5 resolves AutoConnection at emit time, so the slots
# still run in the GUI thread.
_RUN_THREAD_HAS_QUEUED = 'queued_connections' in inspect.signature(_openlp_run_thread).parameters


def _start_worker_thread(worker, thread_name, queued_connections=()):
    """
    Start *worker* on *thread_name*, wiring ``(signal, slot)`` pairs so the
    slots run in the GUI thread on any supported OpenLP version.
    """
    if _RUN_THREAD_HAS_QUEUED:
        _openlp_run_thread(worker, thread_name, queued_connections=queued_connections)
    else:
        for signal, slot in queued_connections:
            signal.connect(slot)
        _openlp_run_thread(worker, thread_name)


# The only archive members a download ever reads: the songs database (at
# either location :func:`songmerge.find_songs_db` checks) and the sync
# metadata.  Anything else in a downloaded archive is ignored -- a backup
# comes from a shared cloud folder and must never be able to write
# arbitrary files (e.g. a plugin under ``contrib/plugins/``) onto this
# machine.
SYNC_ARCHIVE_MEMBERS = ('songs/songs.sqlite', 'songs.sqlite', CLOUDSYNC_META_FILENAME)

SyncResult = namedtuple('SyncResult', ['success', 'action', 'message'])
# action is one of: 'skipped', 'uploaded', 'downloaded', 'in-sync', 'error'

STARTUP_THREAD_NAME = 'cloudsync_startup'
SONG_THREAD_NAME = 'cloudsync_song'
MANUAL_THREAD_NAME = 'cloudsync_manual'
AUTH_THREAD_NAME = 'cloudsync_auth'


class SyncController(QtCore.QObject):
    """
    Coordinates automatic cloud synchronisation of the OpenLP library.

    :param settings: The OpenLP settings object (values under ``cloudsync/``).
    """
    sync_finished = QtCore.Signal(bool, str)

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self._sync_running = False
        self._debounce_timer = QtCore.QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._on_debounce_timeout)
        # Set by _merge_and_upload when a merge rewrote the live songs
        # database; consumed by _on_worker_finished, which then tries to
        # refresh the songs list in place instead of asking for a restart.
        self.merge_applied = False

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    @property
    def data_dir(self):
        """The OpenLP data directory being synced."""
        return AppLocation.get_data_path()

    @property
    def section_dir(self):
        """This plugin's working directory (tokens, state, staging)."""
        return AppLocation.get_section_data_path('cloudsync')

    @property
    def token_path(self):
        return self.section_dir / 'token.json'

    @property
    def enabled(self):
        return bool(self.settings.value('cloudsync/enabled'))

    @property
    def provider_id(self):
        return self.settings.value('cloudsync/provider') or 'googledrive'

    @property
    def drive_folder(self):
        return self.settings.value('cloudsync/drive folder') or 'OpenLP'

    @property
    def drive_folder_ref(self):
        """
        The folder reference passed to the provider: ``'id:<folder-id>'``
        when the folder picker chose one (stable across renames), otherwise
        the plain folder name (looked up, creating it when missing).
        """
        folder_id = self.settings.value('cloudsync/drive folder id')
        if folder_id:
            return 'id:{fid}'.format(fid=folder_id)
        return self.drive_folder

    @property
    def keep_backups(self):
        try:
            return int(self.settings.value('cloudsync/keep backups') or 10)
        except (TypeError, ValueError):
            return 10

    def is_configured(self):
        """
        Check whether sync can run: enabled and a usable OAuth token cached.
        """
        if not self.enabled:
            return False
        return auth_module.has_valid_token(self.token_path)

    # ------------------------------------------------------------------
    # GUI-thread entry points (called from plugin hooks / menu / tab)
    # ------------------------------------------------------------------
    def startup_sync(self):
        """
        Kick off the automatic startup sync in a background thread.
        Called from the plugin's ``app_startup`` hook.
        """
        if not bool(self.settings.value('cloudsync/sync on startup')):
            log.debug('Cloud sync on startup is disabled')
            return
        if self._sync_running or not is_thread_finished(STARTUP_THREAD_NAME):
            log.debug('Cloud sync already running, skipping startup sync')
            return
        if not self.is_configured():
            log.info('Cloud sync not configured, skipping startup sync')
            return
        from .worker import StartupSyncWorker
        self._sync_running = True
        worker = StartupSyncWorker(self)
        _start_worker_thread(worker, STARTUP_THREAD_NAME, queued_connections=[
            (worker.status_message, self._on_status_message),
            (worker.finished, self._on_worker_finished),
        ])

    def on_song_changed(self, song_id=None):
        """
        Handle the ``song_changed`` registry event.  Uploads are debounced
        so rapid successive saves collapse into a single sync.

        :param song_id: Id of the created/updated song (unused, accepted for
            the registry event signature).
        """
        if not self.enabled or not bool(self.settings.value('cloudsync/sync on song change')):
            return
        try:
            debounce_ms = int(self.settings.value('cloudsync/debounce seconds') or 60) * 1000
        except (TypeError, ValueError):
            debounce_ms = 60000
        log.debug('Song changed, scheduling cloud sync in %d ms', debounce_ms)
        self._debounce_timer.start(max(debounce_ms, 5000))

    def _on_debounce_timeout(self):
        if self._sync_running or not is_thread_finished(SONG_THREAD_NAME):
            log.debug('Cloud sync already running, re-scheduling song-change sync')
            self._debounce_timer.start(30000)
            return
        if not self.is_configured():
            return
        from .worker import SongChangeSyncWorker
        self._sync_running = True
        worker = SongChangeSyncWorker(self)
        _start_worker_thread(worker, SONG_THREAD_NAME, queued_connections=[
            (worker.status_message, self._on_status_message),
            (worker.finished, self._on_worker_finished),
        ])

    def sync_now(self):
        """
        User-triggered sync: upload local changes, then pull remote changes.
        """
        if self._sync_running:
            self._on_status_message(translate('CloudSync', 'Sync already running...'))
            return
        if not self.is_configured():
            self._on_status_message(translate('CloudSync', 'Cloud sync is not configured.'))
            return
        from .worker import ManualSyncWorker
        self._sync_running = True
        worker = ManualSyncWorker(self)
        # ManualSyncWorker is not yet registered for the finished signal below
        # if a previous thread with the same name exists; run_thread guards it.
        _start_worker_thread(worker, MANUAL_THREAD_NAME, queued_connections=[
            (worker.status_message, self._on_status_message),
            (worker.finished, self._on_worker_finished),
        ])

    def authenticate(self):
        """
        Start the OAuth flow in a background thread.  The settings tab
        connects to :attr:`sync_finished` for the outcome.
        """
        if not is_thread_finished(AUTH_THREAD_NAME):
            return
        from .worker import AuthWorker
        worker = AuthWorker(self)
        _start_worker_thread(worker, AUTH_THREAD_NAME, queued_connections=[
            (worker.status_message, self._on_status_message),
            (worker.finished, self._on_worker_finished),
        ])

    def _on_status_message(self, message):
        main_window = Registry().get('main_window')
        if main_window:
            main_window.show_status_message('Cloud Sync: {msg}'.format(msg=message))
        log.info('Cloud sync status: %s', message)

    def _on_worker_finished(self, success, message):
        self._sync_running = False
        if success and self.merge_applied:
            # This slot runs on the GUI thread (queued connection), so it is
            # safe to drive the songs UI here: refresh the list in place so
            # merged songs appear without restarting OpenLP.
            self.merge_applied = False
            if refresh_songs_ui():
                message = '{msg} {extra}'.format(
                    msg=message,
                    extra=translate('CloudSync', 'The song list has been refreshed.'))
            else:
                message = '{msg} {extra}'.format(
                    msg=message,
                    extra=translate('CloudSync',
                                    'Restart OpenLP to see them in the song list.'))
        self._on_status_message(message)
        self.sync_finished.emit(success, message)

    # ------------------------------------------------------------------
    # Worker-thread operations
    # ------------------------------------------------------------------
    def _connect_provider(self):
        """
        Load credentials and return a connected provider.

        :return: A connected :class:`SyncProvider`.
        """
        credentials = auth_module.load_credentials(self.token_path)
        if credentials is None:
            raise auth_module.AuthenticationError(
                'No valid cloud credentials. Connect from Settings > Cloud Sync.')
        provider = get_provider(self.provider_id)
        provider.connect(credentials)
        return provider

    def perform_authentication(self):
        """
        Run the OAuth flow (blocking).  Called from the auth worker thread.

        The OAuth client is always the ``client.json`` bundled with the
        plugin; there is no user-supplied client file.

        :return: The obtained credentials.
        """
        return auth_module.run_oauth_flow(self.token_path)

    def perform_upload(self, status_callback=None):
        """
        Snapshot the library and upload it (worker thread).

        :param status_callback: Optional callable receiving status strings.
        :return: :class:`SyncResult`.
        """
        def status(message):
            log.info('Cloud sync upload: %s', message)
            if status_callback:
                status_callback(message)

        work_dir = Path(tempfile.mkdtemp(prefix='openlp-cloudsync-'))
        try:
            status(translate('CloudSync', 'Creating library snapshot...'))
            fingerprint = compute_fingerprint(self.data_dir)
            archive_path, metadata = create_library_archive(self.data_dir, work_dir=work_dir)
            status(translate('CloudSync', 'Uploading library snapshot...'))
            provider = self._connect_provider()
            backup = provider.upload_backup(archive_path, metadata, self.drive_folder_ref)
            provider.prune_backups(self.drive_folder_ref, self.keep_backups)
            record_upload(self.data_dir, fingerprint, metadata)
            try:
                from .songmerge import save_merge_base
                save_merge_base(self.data_dir)
            except OSError:
                log.exception('Cloud sync: could not save merge base after upload')
            message = translate('CloudSync', 'Library synced to cloud ({name}).').format(name=backup.name)
            status(message)
            return SyncResult(True, 'uploaded', message)
        except Exception as error:
            log.exception('Cloud sync upload failed')
            return SyncResult(False, 'error', translate('CloudSync', 'Upload failed: {error}').format(error=error))
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _newest_remote(self, provider):
        backups = provider.list_backups(self.drive_folder_ref)
        return backups[0] if backups else None

    def _is_remote_newer(self, remote, state):
        if remote is None:
            return False
        if not remote.created_utc:
            return False
        if remote.sha256 and remote.sha256 == state.get('remote_sha256'):
            # Seen before -- but was it ever applied?  v12 recorded staged
            # downloads as seen without applying them, so a seen-but-never-
            # applied remote must be downloaded again until it actually
            # lands in the local library.
            if state.get('applied_remote_sha256') != remote.sha256:
                return True
            return False
        if remote.name and remote.name == state.get('remote_name'):
            return False
        last_seen = state.get('remote_created_utc') or ''
        return remote.created_utc > last_seen

    def perform_remote_check(self, provider, status_callback=None):
        """
        Check for a newer remote backup, download it, and apply it live:
        its songs are merged into the running library (three-way when a
        merge base exists) and the song list refreshes in place.  No restart
        is needed.

        :return: :class:`SyncResult`.
        """
        def status(message):
            log.info('Cloud sync remote check: %s', message)
            if status_callback:
                status_callback(message)

        try:
            state = load_state(self.data_dir)
            remote = self._newest_remote(provider)
            if not self._is_remote_newer(remote, state):
                if remote is not None:
                    record_remote_seen(self.data_dir, remote)
                return SyncResult(True, 'in-sync',
                                  translate('CloudSync', 'Library is in sync with the cloud.'))
            status(translate('CloudSync', 'Newer library found on {host}, downloading...').format(
                host=remote.hostname))
            work_dir = Path(tempfile.mkdtemp(prefix='openlp-cloudsync-'))
            try:
                download_path = work_dir / 'download.zip'
                self._download_verified(provider, remote, download_path)
                message = self._apply_downloaded_archive(remote, download_path, status)
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)
            status(message)
            return SyncResult(True, 'downloaded', message)
        except Exception as error:
            log.exception('Cloud sync remote check failed')
            return SyncResult(False, 'error', translate('CloudSync', 'Download failed: {error}').format(error=error))

    @staticmethod
    def _download_verified(provider, remote, download_path):
        """
        Download *remote* to *download_path* and check it against the
        checksum recorded at upload time.

        Backups uploaded by other tools carry no checksum; those are
        accepted unchecked.

        :raises ValueError: When the downloaded file does not match.
        """
        provider.download_backup(remote, download_path)
        if remote.sha256 and compute_sha256(download_path) != remote.sha256:
            raise ValueError('Downloaded backup {name} failed its checksum check; '
                             'not applying it'.format(name=remote.name))

    @staticmethod
    def _extract_sync_members(archive_path, extract_dir):
        """
        Extract only the members listed in :data:`SYNC_ARCHIVE_MEMBERS`
        from a downloaded archive into *extract_dir*.
        """
        with zipfile.ZipFile(str(archive_path), 'r') as archive:
            names = set(archive.namelist())
            for member in SYNC_ARCHIVE_MEMBERS:
                if member in names:
                    archive.extract(member, str(extract_dir))

    @staticmethod
    def _remote_base_identities(extract_dir):
        """
        Read the uploader's base song identities from an extracted backup's
        sync metadata.

        :return: A set of identity key strings the uploader had at its own
            last sync, or None when the backup carries no (readable) sync
            metadata.  None means "unknown": the merge must not treat a
            missing song as a remote deletion.
        """
        import json
        meta_path = Path(extract_dir) / CLOUDSYNC_META_FILENAME
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if 'base_identities' not in meta:
            return None
        return set(meta['base_identities'] or [])

    def _apply_downloaded_archive(self, remote, archive_path, status_callback=None):
        """
        Apply a downloaded library archive live: merge its songs into the
        local songs database (three-way when a merge base exists), then
        record the new sync state and merge base.  Nothing else from the
        archive is written to disk.

        :param remote: :class:`RemoteBackup` describing the archive.
        :param archive_path: Path of the downloaded zip file.
        :return: Human-readable summary message.
        """
        from .songmerge import merge_base_path, merge_songs_databases, save_merge_base
        data_dir = Path(self.data_dir)
        report = None
        with tempfile.TemporaryDirectory(prefix='openlp-cloudsync-apply-') as tmp_dir:
            extract_dir = Path(tmp_dir) / 'extracted'
            extract_dir.mkdir()
            self._extract_sync_members(archive_path, extract_dir)
            remote_songs_db = find_songs_db(extract_dir)
            local_songs_db = find_songs_db(data_dir)
            if remote_songs_db is not None and local_songs_db is not None:
                base_path = merge_base_path(data_dir)
                report = merge_songs_databases(
                    local_songs_db, remote_songs_db,
                    base_path if base_path.is_file() else None,
                    remote_base_identities=self._remote_base_identities(extract_dir))
                # The merge rewrote the live songs database; flag it so the
                # GUI-thread finish handler refreshes the songs list in place.
                self.merge_applied = True
                log.info('Cloud sync: applied download from %s: %d added, %d updated, %d deleted',
                         remote.hostname, report['added'], report['updated'], report['deleted'])
            else:
                log.warning('Cloud sync: no songs database to merge (remote: %s, local: %s)',
                            remote_songs_db, local_songs_db)
        # The local library now matches the applied remote backup: record it
        # as both the local fingerprint and the newest seen remote backup, so
        # the next sync does not re-download the backup just applied.
        record_upload(data_dir, compute_fingerprint(data_dir), {
            'name': remote.name,
            'hostname': remote.hostname,
            'created_utc': remote.created_utc,
            'sha256': remote.sha256,
        })
        try:
            save_merge_base(data_dir)
        except OSError:
            log.exception('Cloud sync: could not save merge base after applying download')
        if report is not None:
            lines = [translate('CloudSync',
                               'Downloaded library from {host}: {added} new, '
                               '{updated} updated songs.').format(
                host=remote.hostname, added=report['added'], updated=report['updated'])]
            if report['deleted']:
                lines.append(translate('CloudSync', '{deleted} deleted.').format(
                    deleted=report['deleted']))
            lines.extend(report['details'][:10])
            return ' '.join(lines)
        return translate('CloudSync', 'Downloaded library from {host}.').format(
            host=remote.hostname)

    def _apply_pending_restore(self, marker, status_callback=None):
        """
        Apply a v12-staged download through the live path.

        v12 staged whole-library downloads for application at the next
        restart, but the released OpenLP app has no hook to apply them, so
        staged restores were never applied.  v13 applies any leftover
        staged download live on the next sync instead.

        :param dict marker: The pending-restore marker from
            :func:`restore.has_pending_restore`.
        :return: Human-readable summary message.
        """
        from .restore import STAGED_ARCHIVE_FILENAME, pending_restore_archive_path
        backup_meta = marker.get('backup') or {}
        staged = pending_restore_archive_path(self.data_dir, marker)
        if staged is None or not staged.is_file():
            raise FileNotFoundError('Staged restore archive {name} is missing'.format(
                name=marker.get('staged_archive', STAGED_ARCHIVE_FILENAME)))
        expected_sha256 = backup_meta.get('sha256')
        if expected_sha256 and compute_sha256(staged) != expected_sha256:
            raise ValueError('Staged archive checksum mismatch; discarding restore')
        remote = RemoteBackup(
            remote_id=None,
            name=backup_meta.get('name') or staged.name,
            hostname=backup_meta.get('hostname') or 'unknown',
            created_utc=backup_meta.get('created_utc') or '',
            sha256=backup_meta.get('sha256') or '',
            size_bytes=backup_meta.get('size_bytes') or 0,
            openlp_version=backup_meta.get('openlp_version') or 'unknown',
        )
        return self._apply_downloaded_archive(remote, staged, status_callback)

    def perform_startup_sync(self, status_callback=None):
        """
        Automatic startup sync (worker thread): upload local changes,
        download newer remote libraries, song-level merge on conflict.
        """
        return self._sync_flow(status_callback)

    def perform_manual_sync(self, status_callback=None):
        """
        Manual "Sync now" (worker thread): same decision flow as the
        startup sync -- check the remote side first so a newer cloud
        library is merged rather than overwritten.
        """
        return self._sync_flow(status_callback)

    def _sync_flow(self, status_callback=None):
        """
        Shared sync decision flow: upload local changes, download newer
        remote libraries, and song-level merge when both sides changed.
        """
        try:
            provider = self._connect_provider()
        except Exception as error:
            log.exception('Cloud sync: could not connect')
            return SyncResult(False, 'error', str(error))
        pending = has_pending_restore(self.data_dir)
        if pending is not None:
            # Leftover from v12, which staged whole-library downloads for a
            # restart that the released app never applied: apply it through
            # the live path now, then continue with the normal flow.
            log.info('Cloud sync: applying leftover staged download from v12')
            try:
                message = self._apply_pending_restore(pending, status_callback)
            except Exception as error:
                log.exception('Cloud sync: could not apply staged download; discarding it')
                discard_pending_restore(self.data_dir)
                return SyncResult(False, 'error', str(error))
            discard_pending_restore(self.data_dir)
            if status_callback:
                status_callback(message)
            return SyncResult(True, 'downloaded', message)
        state = load_state(self.data_dir)
        local_fingerprint = compute_fingerprint(self.data_dir)
        local_changed = state.get('local_fingerprint') != local_fingerprint
        try:
            remote = self._newest_remote(provider)
        except Exception as error:
            log.exception('Cloud sync: could not list remote backups')
            return SyncResult(False, 'error', str(error))
        remote_newer = self._is_remote_newer(remote, state)

        if remote_newer and local_changed:
            # Conflict: both sides changed since the last sync.  Merge the
            # remote songs into the local library, then upload the merged
            # result so every machine converges.  If anything goes wrong,
            # fall back to last-write-wins below.
            log.info('Cloud sync conflict: local and remote libraries both changed; merging')
            try:
                return self._merge_and_upload(provider, remote, status_callback)
            except Exception:
                log.exception('Cloud sync merge failed; falling back to last-write-wins')
        if remote_newer and (not local_changed or (remote.created_utc > (state.get('local_synced_utc') or ''))):
            return self.perform_remote_check(provider, status_callback)
        if local_changed:
            return self.perform_upload(status_callback)
        if remote is not None:
            record_remote_seen(self.data_dir, remote)
        return SyncResult(True, 'in-sync', translate('CloudSync', 'Library is in sync with the cloud.'))

    def _merge_and_upload(self, provider, remote, status_callback=None):
        """
        Download the remote backup, merge its songs into the local library,
        and upload the merged result.

        :return: :class:`SyncResult`.
        """
        def status(message):
            log.info('Cloud sync merge: %s', message)
            if status_callback:
                status_callback(message)

        from .songmerge import merge_base_path, merge_songs_databases
        local_songs_db = find_songs_db(self.data_dir)
        if local_songs_db is None:
            # Nothing local to merge into -- fall back to plain download.
            raise ValueError('No local songs database to merge into')
        status(translate('CloudSync', 'Downloading cloud library for merge...'))
        work_dir = Path(tempfile.mkdtemp(prefix='openlp-cloudsync-merge-'))
        try:
            download_path = work_dir / 'download.zip'
            self._download_verified(provider, remote, download_path)
            extract_dir = work_dir / 'remote'
            extract_dir.mkdir()
            self._extract_sync_members(download_path, extract_dir)
            remote_songs_db = find_songs_db(extract_dir)
            if remote_songs_db is None:
                raise ValueError('Remote backup has no songs database')
            status(translate('CloudSync', 'Merging songs...'))
            base_path = merge_base_path(self.data_dir)
            report = merge_songs_databases(local_songs_db, remote_songs_db,
                                           base_path if base_path.is_file() else None,
                                           remote_base_identities=self._remote_base_identities(extract_dir))
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        record_remote_seen(self.data_dir, remote)
        # The merge rewrote the live songs database; flag it so the
        # GUI-thread finish handler can refresh the songs list in place.
        self.merge_applied = True
        result = self.perform_upload(status_callback)
        if result.success:
            lines = [translate('CloudSync',
                               'Merged {added} new and {updated} updated songs from {host}.').format(
                added=report['added'], updated=report['updated'], host=remote.hostname)]
            if report['deleted']:
                lines.append(translate('CloudSync', '{deleted} deleted.').format(
                    deleted=report['deleted']))
            lines.extend(report['details'][:10])
            message = ' '.join(lines)
            status(message)
            return SyncResult(True, 'merged', message)
        return result
