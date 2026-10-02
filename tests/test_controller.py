"""
Tests for the Cloud Sync plugin's SyncController decision logic.

A fake in-memory :class:`SyncProvider` stands in for Google Drive so the
"should I download?" logic and the upload/download flows can be exercised
without network access.
"""

import io
import zipfile
from pathlib import Path

import pytest

from openlp.plugins.cloudsync.lib.providers import RemoteBackup, SyncProvider
from openlp.plugins.cloudsync.lib.state import load_state, record_remote_seen
from openlp.plugins.cloudsync.lib.synccontroller import SyncController


class FakeProvider(SyncProvider):
    """In-memory SyncProvider for tests."""

    def __init__(self, backups=None, list_error=None, zip_songs=(), zip_files=None,
                 corrupt_download=False):
        self._backups = list(backups or [])
        self._list_error = list_error
        # Songs (make_db dicts) baked into songs/songs.sqlite in the
        # downloaded zip, plus extra {arcname: bytes} entries.
        self._zip_songs = list(zip_songs)
        self._zip_files = dict(zip_files or {})
        # The zip is built at download time, so its real checksum is only
        # known then: stamp it onto the backup (as the uploader would have),
        # unless the test wants a download that fails verification.
        self._corrupt_download = corrupt_download
        self.uploaded = []
        self.downloaded = []
        self.deleted = []
        self.pruned = []

    def connect(self, credentials):
        self.credentials = credentials

    def is_connected(self):
        return True

    def list_backups(self, folder_name):
        if self._list_error is not None:
            raise self._list_error
        return list(self._backups)

    def upload_backup(self, archive_path, metadata, folder_name):
        backup = RemoteBackup(
            remote_id='fake-id-{}'.format(len(self.uploaded)),
            name=metadata['name'],
            hostname=metadata['hostname'],
            created_utc=metadata['created_utc'],
            sha256=metadata['sha256'],
            size_bytes=metadata['size_bytes'],
            openlp_version=metadata['openlp_version'],
        )
        self.uploaded.append((Path(archive_path), metadata, folder_name))
        return backup

    def download_backup(self, backup, destination_path):
        import tempfile
        from tests.openlp_plugins.cloudsync.test_songmerge import make_db
        destination_path = Path(destination_path)
        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(str(destination_path), 'w') as archive:
                if self._zip_songs:
                    db_path = Path(tmp_dir) / 'songs.sqlite'
                    make_db(db_path, self._zip_songs)
                    archive.write(str(db_path), 'songs/songs.sqlite')
                for arcname, data in self._zip_files.items():
                    archive.writestr(arcname, data)
        if backup.sha256 and not self._corrupt_download:
            from openlp.plugins.cloudsync.lib.backup import compute_sha256
            backup.sha256 = compute_sha256(destination_path)
        self.downloaded.append((backup, destination_path))

    def delete_backup(self, backup):
        self.deleted.append(backup)

    def prune_backups(self, folder_name, keep):
        self.pruned.append((folder_name, keep))


def make_backup(name='openlp-library-remote-2026.zip', hostname='remote-pc',
                created_utc='2026-09-28T10:00:00Z', sha256='abc123', size_bytes=64):
    return RemoteBackup('fake-id', name, hostname, created_utc, sha256, size_bytes)


class TestableController(SyncController):
    """SyncController pointed at a tmp data dir instead of the real one."""

    def __init__(self, settings, data_dir):
        self._test_data_dir = Path(data_dir)
        super().__init__(settings)

    @property
    def data_dir(self):
        return self._test_data_dir


@pytest.fixture
def controller(qapp, settings, tmp_path):
    return TestableController(settings, tmp_path)


class TestIsRemoteNewer:
    def test_none_remote(self, controller, tmp_path):
        assert controller._is_remote_newer(None, load_state(tmp_path)) is False

    def test_remote_without_timestamp(self, controller, tmp_path):
        backup = make_backup(created_utc='')
        assert controller._is_remote_newer(backup, load_state(tmp_path)) is False

    def test_seen_but_never_applied_sha256_is_newer(self, controller, tmp_path):
        # v12 recorded staged downloads as seen without applying them:
        # seen-but-never-applied counts as newer so it gets downloaded.
        backup = make_backup(sha256='seen-sha')
        record_remote_seen(tmp_path, backup)
        state = load_state(tmp_path)
        assert controller._is_remote_newer(backup, state) is True

    def test_applied_sha256_is_not_newer(self, controller, tmp_path):
        from openlp.plugins.cloudsync.lib.state import record_upload
        backup = make_backup(sha256='seen-sha')
        record_upload(tmp_path, 'fp', {
            'name': backup.name, 'hostname': backup.hostname,
            'created_utc': backup.created_utc, 'sha256': backup.sha256,
        })
        state = load_state(tmp_path)
        assert controller._is_remote_newer(backup, state) is False

    def test_already_seen_name_without_sha(self, controller, tmp_path):
        # Legacy backups without a checksum still dedupe by name.
        backup = make_backup(sha256='', created_utc='2026-09-28T11:00:00Z')
        record_remote_seen(tmp_path, backup)
        state = load_state(tmp_path)
        assert controller._is_remote_newer(backup, state) is False

    def test_older_remote_is_not_newer(self, controller, tmp_path):
        seen = make_backup(created_utc='2026-09-28T12:00:00Z', sha256='seen')
        record_remote_seen(tmp_path, seen)
        older = make_backup(name='openlp-library-remote-old.zip', created_utc='2026-09-28T11:00:00Z',
                            sha256='other')
        assert controller._is_remote_newer(older, load_state(tmp_path)) is False

    def test_newer_remote_with_no_state(self, controller, tmp_path):
        backup = make_backup(created_utc='2026-09-28T10:00:00Z')
        assert controller._is_remote_newer(backup, load_state(tmp_path)) is True

    def test_newer_remote_beats_last_seen(self, controller, tmp_path):
        seen = make_backup(created_utc='2026-09-28T09:00:00Z', sha256='seen')
        record_remote_seen(tmp_path, seen)
        newer = make_backup(name='openlp-library-remote-new.zip', created_utc='2026-09-28T10:00:00Z',
                            sha256='new')
        assert controller._is_remote_newer(newer, load_state(tmp_path)) is True


class TestRemoteBaseIdentities:
    def test_identities_when_meta_carries_them(self, tmp_path):
        import json
        (tmp_path / 'cloudsync-meta.json').write_text(
            json.dumps({'format': 1, 'base_identities': ['a\x1fJohn', 'b\x1fMary']}))
        assert SyncController._remote_base_identities(tmp_path) == {'a\x1fJohn', 'b\x1fMary'}

    def test_none_when_meta_has_no_identities(self, tmp_path):
        # Backups from older plugin versions carry no base identities:
        # the remote's history is unknown, so nothing may be deleted.
        import json
        (tmp_path / 'cloudsync-meta.json').write_text(json.dumps({'format': 1, 'has_base': True}))
        assert SyncController._remote_base_identities(tmp_path) is None

    def test_none_when_meta_missing(self, tmp_path):
        assert SyncController._remote_base_identities(tmp_path) is None

    def test_none_when_meta_corrupt(self, tmp_path):
        (tmp_path / 'cloudsync-meta.json').write_text('not json')
        assert SyncController._remote_base_identities(tmp_path) is None

    def test_empty_list_means_remote_base_was_empty(self, tmp_path):
        # A present-but-empty list is known history: the uploader had no
        # songs at its last sync, so none of ours were deleted remotely.
        import json
        (tmp_path / 'cloudsync-meta.json').write_text(
            json.dumps({'format': 1, 'base_identities': []}))
        assert SyncController._remote_base_identities(tmp_path) == set()


class TestPerformRemoteCheck:
    def test_empty_remote_is_in_sync(self, controller, tmp_path):
        result = controller.perform_remote_check(FakeProvider())
        assert result.success is True
        assert result.action == 'in-sync'

    def test_seen_but_never_applied_remote_is_redownloaded(self, controller, tmp_path):
        # v12 recorded staged downloads as "seen" without applying them.
        # v13 must download such a remote again until it actually lands.
        from tests.openlp_plugins.cloudsync.test_songmerge import make_db, song_titles
        from openlp.plugins.cloudsync.lib.state import record_upload
        songs_dir = tmp_path / 'songs'
        songs_dir.mkdir()
        make_db(songs_dir / 'songs.sqlite',
                [{'id': 1, 'title': 'Local Song', 'authors': ['Me'],
                  'lyrics': 'local words', 'last_modified': '2026-09-01 00:00:00'}])
        backup = make_backup()
        record_remote_seen(tmp_path, backup)  # v12-style: seen, never applied
        provider = FakeProvider(
            [backup],
            zip_songs=[{'id': 1, 'title': 'Remote Song', 'authors': ['You'],
                        'lyrics': 'remote words', 'last_modified': '2026-09-02 00:00:00'}],
        )
        result = controller.perform_remote_check(provider)
        assert result.success is True
        assert result.action == 'downloaded'
        assert song_titles(songs_dir / 'songs.sqlite') == ['Local Song', 'Remote Song']
        state = load_state(tmp_path)
        assert state['applied_remote_sha256'] == backup.sha256
        # Once applied, the same remote reports in-sync.
        assert controller.perform_remote_check(provider).action == 'in-sync'

    def test_applied_remote_is_in_sync(self, controller, tmp_path):
        from openlp.plugins.cloudsync.lib.state import record_upload
        backup = make_backup()
        record_upload(tmp_path, 'fingerprint', {
            'name': backup.name, 'hostname': backup.hostname,
            'created_utc': backup.created_utc, 'sha256': backup.sha256,
        })
        result = controller.perform_remote_check(FakeProvider([backup]))
        assert result.success is True
        assert result.action == 'in-sync'

    def test_newer_remote_is_downloaded_and_applied_live(self, controller, tmp_path):
        from tests.openlp_plugins.cloudsync.test_songmerge import make_db, song_titles
        from openlp.plugins.cloudsync.lib.backup import compute_fingerprint
        # Local library (real OpenLP layout) with one song.
        songs_dir = tmp_path / 'songs'
        songs_dir.mkdir()
        make_db(songs_dir / 'songs.sqlite',
                [{'id': 1, 'title': 'Local Song', 'authors': ['Me'],
                  'lyrics': 'local words', 'last_modified': '2026-09-01 00:00:00'}])
        backup = make_backup()
        provider = FakeProvider(
            [backup],
            zip_songs=[{'id': 1, 'title': 'Remote Song', 'authors': ['You'],
                        'lyrics': 'remote words', 'last_modified': '2026-09-02 00:00:00'}],
        )
        result = controller.perform_remote_check(provider)
        assert result.success is True
        assert result.action == 'downloaded'
        assert 'remote-pc' in result.message
        # The remote song was merged into the LIVE database: no restart,
        # no staged files.
        assert song_titles(songs_dir / 'songs.sqlite') == ['Local Song', 'Remote Song']
        section = tmp_path / 'cloudsync'
        assert not (section / 'staged-library.zip').exists()
        assert not (section / 'pending-restore.json').exists()
        # The merge flag is set so the GUI thread refreshes the song list.
        assert controller.merge_applied is True
        # Fake provider was asked to download the newest backup.
        assert provider.downloaded and provider.downloaded[0][0] is backup
        # State records the seen remote and the new local fingerprint, so a
        # follow-up check reports in-sync instead of re-downloading.
        state = load_state(tmp_path)
        assert state['remote_name'] == backup.name
        assert state['remote_sha256'] == backup.sha256
        assert state['local_fingerprint'] == compute_fingerprint(tmp_path)
        assert controller.perform_remote_check(provider).action == 'in-sync'

    def test_download_writes_nothing_but_the_merge(self, controller, tmp_path):
        # A backup comes from a shared cloud folder: extra archive members
        # must never land on disk -- above all a plugin, which OpenLP would
        # load (and run) on its next start.
        (tmp_path / 'songs').mkdir()
        backup = make_backup()
        provider = FakeProvider(
            [backup],
            zip_files={
                'themes/custom.otz': b'theme-bytes',
                'contrib/plugins/evil/evilplugin.py': b'import os',
                'contrib/plugins/cloudsync/cloudsyncplugin.py': b'import os',
                'CloudSync/token.json': b'{}',
                'bibles/bible.sqlite': b'sqlite-bytes',
            },
        )
        result = controller.perform_remote_check(provider)
        assert result.success is True
        assert not (tmp_path / 'themes').exists()
        assert not (tmp_path / 'contrib').exists()
        assert not (tmp_path / 'bibles').exists()
        assert not (tmp_path / 'CloudSync' / 'token.json').exists()
        assert not (tmp_path / 'cloudsync' / 'token.json').exists()

    def test_download_without_songs_db_succeeds_without_merge(self, controller, tmp_path):
        (tmp_path / 'songs').mkdir()
        backup = make_backup()
        provider = FakeProvider([backup], zip_files={'themes/custom.otz': b'theme-bytes'})
        result = controller.perform_remote_check(provider)
        assert result.success is True
        assert 'remote-pc' in result.message
        assert controller.merge_applied is False

    def test_download_with_bad_checksum_is_not_applied(self, controller, tmp_path):
        from tests.openlp_plugins.cloudsync.test_songmerge import make_db, song_titles
        songs_dir = tmp_path / 'songs'
        songs_dir.mkdir()
        make_db(songs_dir / 'songs.sqlite',
                [{'id': 1, 'title': 'Local Song', 'authors': ['Me'],
                  'lyrics': 'local words', 'last_modified': '2026-09-01 00:00:00'}])
        backup = make_backup(sha256='not-the-real-checksum')
        provider = FakeProvider(
            [backup], corrupt_download=True,
            zip_songs=[{'id': 9, 'title': 'Remote Song', 'authors': ['You'],
                        'lyrics': 'remote words', 'last_modified': '2026-09-02 00:00:00'}])
        result = controller.perform_remote_check(provider)
        assert result.success is False
        assert 'checksum' in result.message
        assert song_titles(songs_dir / 'songs.sqlite') == ['Local Song']
        assert load_state(tmp_path)['applied_remote_sha256'] is None

    def test_download_path_ignores_remote_file_name(self, controller, tmp_path):
        # Drive file names are remote data: a name with path separators
        # must not steer where the download is written.
        (tmp_path / 'songs').mkdir()
        backup = make_backup(name='openlp-library-/../../escaped.zip')
        provider = FakeProvider([backup])
        controller.perform_remote_check(provider)
        written = provider.downloaded[0][1]
        assert written.name == 'download.zip'

    def test_download_without_remote_base_never_deletes(self, controller, tmp_path):
        # Regression: a remote backup whose uploader never synced (no
        # has_base in its metadata) must merge additively -- its missing
        # songs are not deletions.
        import json
        from openlp.plugins.cloudsync.lib.songmerge import save_merge_base
        from tests.openlp_plugins.cloudsync.test_songmerge import make_db, song_titles
        songs_dir = tmp_path / 'songs'
        songs_dir.mkdir()
        local_songs = [{'id': 1, 'title': 'Local Song', 'authors': ['Me'],
                        'lyrics': 'local words', 'last_modified': '2026-09-01 00:00:00'}]
        make_db(songs_dir / 'songs.sqlite', local_songs)
        assert save_merge_base(tmp_path) is True  # local side has sync history
        backup = make_backup()
        provider = FakeProvider(
            [backup],
            zip_songs=[{'id': 9, 'title': 'Remote Song', 'authors': ['You'],
                        'lyrics': 'remote words', 'last_modified': '2026-09-02 00:00:00'}],
            zip_files={'cloudsync-meta.json': json.dumps({'format': 1, 'has_base': False})},
        )
        result = controller.perform_remote_check(provider)
        assert result.success is True
        # The local song survives; the remote song is added.
        assert song_titles(songs_dir / 'songs.sqlite') == ['Local Song', 'Remote Song']
        # The metadata file itself is never copied into the data dir.
        assert not (tmp_path / 'cloudsync-meta.json').exists()

    def test_download_with_remote_base_propagates_deletion(self, controller, tmp_path):
        # Same shape, but the remote backup's metadata lists the uploader's
        # own base identities including this song: its absence really was a
        # deletion, so the deletion propagates.
        import json
        import tempfile
        from openlp.plugins.cloudsync.lib.songmerge import _identity_key_string, save_merge_base
        from tests.openlp_plugins.cloudsync.test_songmerge import make_db, song_titles
        songs_dir = tmp_path / 'songs'
        songs_dir.mkdir()
        make_db(songs_dir / 'songs.sqlite',
                [{'id': 1, 'title': 'Local Song', 'authors': ['Me'],
                  'lyrics': 'local words', 'last_modified': '2026-09-01 00:00:00'}])
        assert save_merge_base(tmp_path) is True
        backup = make_backup()
        # An empty songs db in the zip (FakeProvider skips empty zip_songs).
        with tempfile.TemporaryDirectory() as td:
            empty_db = Path(td) / 'songs.sqlite'
            make_db(empty_db, [])
            empty_bytes = empty_db.read_bytes()
        provider = FakeProvider(
            [backup],
            zip_files={
                'songs/songs.sqlite': empty_bytes,
                'cloudsync-meta.json': json.dumps({
                    'format': 1, 'has_base': True,
                    'base_identities': [_identity_key_string(('local song', ('Me',)))],
                }),
            },
        )
        result = controller.perform_remote_check(provider)
        assert result.success is True
        assert song_titles(songs_dir / 'songs.sqlite') == []

    def test_status_callback_receives_messages(self, controller, tmp_path):
        messages = []
        controller.perform_remote_check(FakeProvider([make_backup()]), status_callback=messages.append)
        assert messages  # at least the download progress message

    def test_provider_error_returns_error_result(self, controller, tmp_path):
        provider = FakeProvider(list_error=ConnectionError('offline'))
        result = controller.perform_remote_check(provider)
        assert result.success is False
        assert result.action == 'error'


class TestPendingRestoreMigration:
    """A v12 staged download is applied live on the next v13 sync."""

    def test_staged_v12_download_is_applied_live(self, controller, tmp_path, monkeypatch):
        import json
        import tempfile
        import zipfile
        from openlp.plugins.cloudsync.lib.backup import compute_sha256
        from openlp.plugins.cloudsync.lib.restore import has_pending_restore
        from tests.openlp_plugins.cloudsync.test_songmerge import make_db, song_titles
        # Local library with one song (real OpenLP layout).
        songs_dir = tmp_path / 'songs'
        songs_dir.mkdir()
        make_db(songs_dir / 'songs.sqlite',
                [{'id': 1, 'title': 'Local Song', 'authors': ['Me'],
                  'lyrics': 'local words', 'last_modified': '2026-09-01 00:00:00'}])
        # A v12-style staged download: zip + marker in the section dir.
        section = tmp_path / 'cloudsync'
        section.mkdir()
        with tempfile.TemporaryDirectory() as tmp_dir:
            remote_db = Path(tmp_dir) / 'songs.sqlite'
            make_db(remote_db,
                    [{'id': 1, 'title': 'Remote Song', 'authors': ['You'],
                      'lyrics': 'remote words', 'last_modified': '2026-09-02 00:00:00'}])
            staged = section / 'staged-library.zip'
            with zipfile.ZipFile(str(staged), 'w') as archive:
                archive.write(str(remote_db), 'songs/songs.sqlite')
        sha256 = compute_sha256(staged)
        marker = {
            'staged_archive': 'staged-library.zip',
            'backup': {'name': 'openlp-library-remote-2026.zip', 'hostname': 'remote-pc',
                       'created_utc': '2026-09-28T10:00:00Z', 'sha256': sha256},
            'staged_utc': '2026-09-29T10:00:00Z',
        }
        (section / 'pending-restore.json').write_text(json.dumps(marker))
        assert has_pending_restore(tmp_path) is not None

        monkeypatch.setattr(controller, '_connect_provider', lambda: FakeProvider())
        result = controller.perform_startup_sync()

        assert result.success is True
        assert result.action == 'downloaded'
        # Applied live: the remote song is in the running library and the
        # staged files are gone.
        assert song_titles(songs_dir / 'songs.sqlite') == ['Local Song', 'Remote Song']
        assert has_pending_restore(tmp_path) is None
        assert not (section / 'staged-library.zip').exists()
        state = load_state(tmp_path)
        assert state['remote_sha256'] == sha256
        assert state['applied_remote_sha256'] == sha256

    def test_corrupt_staged_download_is_discarded(self, controller, tmp_path, monkeypatch):
        import json
        from openlp.plugins.cloudsync.lib.restore import has_pending_restore
        section = tmp_path / 'cloudsync'
        section.mkdir()
        (section / 'staged-library.zip').write_bytes(b'not a zip')
        marker = {
            'staged_archive': 'staged-library.zip',
            'backup': {'name': 'x.zip', 'hostname': 'remote-pc',
                       'created_utc': '2026-09-28T10:00:00Z', 'sha256': 'wrong'},
            'staged_utc': '2026-09-29T10:00:00Z',
        }
        (section / 'pending-restore.json').write_text(json.dumps(marker))
        monkeypatch.setattr(controller, '_connect_provider', lambda: FakeProvider())
        result = controller.perform_startup_sync()
        assert result.success is False
        assert has_pending_restore(tmp_path) is None


class TestPerformUpload:
    def test_upload_records_state(self, controller, tmp_path, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(controller, '_connect_provider', lambda: provider)
        (tmp_path / 'songs.sqlite').write_bytes(b'data')
        result = controller.perform_upload()
        assert result.success is True
        assert result.action == 'uploaded'
        assert provider.uploaded, 'expected an upload through the fake provider'
        archive_path, metadata, folder = provider.uploaded[0]
        assert metadata['sha256']
        assert folder == controller.drive_folder
        state = load_state(tmp_path)
        assert state['local_fingerprint']
        assert state['local_synced_utc']
        assert state['remote_name'] == metadata['name']

    def test_upload_prunes_old_backups(self, controller, tmp_path, monkeypatch, settings):
        provider = FakeProvider()
        monkeypatch.setattr(controller, '_connect_provider', lambda: provider)
        settings.setValue('cloudsync/keep backups', 3)
        (tmp_path / 'songs.sqlite').write_bytes(b'data')
        controller.perform_upload()
        assert provider.pruned == [(controller.drive_folder, 3)]

    def test_upload_error_returns_error_result(self, controller, tmp_path, monkeypatch):
        monkeypatch.setattr(controller, '_connect_provider',
                            lambda: (_ for _ in ()).throw(RuntimeError('no token')))
        result = controller.perform_upload()
        assert result.success is False
        assert result.action == 'error'


class TestPerformStartupSync:
    def test_uploads_local_changes_when_remote_empty(self, controller, tmp_path, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(controller, '_connect_provider', lambda: provider)
        (tmp_path / 'songs.sqlite').write_bytes(b'data')
        result = controller.perform_startup_sync()
        assert result.success is True
        assert result.action == 'uploaded'

    def test_downloads_newer_remote_when_local_unchanged(self, controller, tmp_path, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(controller, '_connect_provider', lambda: provider)
        (tmp_path / 'songs.sqlite').write_bytes(b'data')
        # First sync records the local fingerprint via upload; clear the
        # remote list for that first run, then present a newer remote.
        assert controller.perform_startup_sync().action == 'uploaded'
        provider.uploaded.clear()
        backup = make_backup(created_utc='2099-01-01T00:00:00Z', sha256='newer-sha')
        provider._backups = [backup]
        result = controller.perform_startup_sync()
        assert result.success is True
        assert result.action == 'downloaded'
        assert not provider.uploaded, 'local unchanged: should not re-upload'

    def test_in_sync_when_nothing_changed(self, controller, tmp_path, monkeypatch):
        provider = FakeProvider()
        monkeypatch.setattr(controller, '_connect_provider', lambda: provider)
        (tmp_path / 'songs.sqlite').write_bytes(b'data')
        assert controller.perform_startup_sync().action == 'uploaded'
        provider.uploaded.clear()
        result = controller.perform_startup_sync()
        assert result.success is True
        assert result.action == 'in-sync'
        assert not provider.uploaded

    def test_connection_failure_returns_error(self, controller, monkeypatch):
        monkeypatch.setattr(controller, '_connect_provider',
                            lambda: (_ for _ in ()).throw(RuntimeError('no credentials')))
        result = controller.perform_startup_sync()
        assert result.success is False
        assert result.action == 'error'


class TestDriveFolderRef:
    def test_plain_name_without_folder_id(self, controller):
        assert controller.drive_folder_ref == 'OpenLP'

    def test_folder_id_prefix_wins_over_name(self, controller, settings):
        settings.setValue('cloudsync/drive folder', 'OpenLP')
        settings.setValue('cloudsync/drive folder id', 'pinned-42')
        assert controller.drive_folder_ref == 'id:pinned-42'

    def test_sync_uses_folder_ref(self, controller, settings, tmp_path, monkeypatch):
        settings.setValue('cloudsync/drive folder id', 'pinned-42')
        provider = FakeProvider()
        monkeypatch.setattr(controller, '_connect_provider', lambda: provider)
        (tmp_path / 'songs.sqlite').write_bytes(b'data')
        controller.perform_upload()
        assert provider.uploaded[0][2] == 'id:pinned-42'
        assert provider.pruned[0][0] == 'id:pinned-42'


class TestMergeFlow:
    def test_conflict_merges_then_uploads(self, controller, settings, tmp_path, monkeypatch):
        import shutil
        import sqlite3
        import zipfile
        from tests.openlp_plugins.cloudsync.test_songmerge import make_db
        # Local library with one song; no state yet -> local counts as changed.
        make_db(tmp_path / 'songs.sqlite',
                [{'id': 1, 'title': 'Local Song', 'authors': ['Me'],
                  'lyrics': 'local words', 'last_modified': '2026-09-01 00:00:00'}])
        # Remote backup zip with a different song.
        remote_dir = tmp_path / 'remote_data'
        remote_dir.mkdir()
        make_db(remote_dir / 'songs.sqlite',
                [{'id': 1, 'title': 'Remote Song', 'authors': ['You'],
                  'lyrics': 'remote words', 'last_modified': '2026-09-02 00:00:00'}])
        remote_zip = tmp_path / 'remote.zip'
        with zipfile.ZipFile(str(remote_zip), 'w') as archive:
            archive.write(str(remote_dir / 'songs.sqlite'), 'songs.sqlite')

        class MergeProvider(FakeProvider):
            def download_backup(self, backup, destination_path):
                shutil.copy(str(remote_zip), str(destination_path))

        from openlp.plugins.cloudsync.lib.backup import compute_sha256
        provider = MergeProvider(backups=[make_backup(created_utc='2026-09-29T22:00:00Z',
                                                      sha256=compute_sha256(remote_zip))])
        monkeypatch.setattr(controller, '_connect_provider', lambda: provider)

        result = controller.perform_startup_sync()

        assert result.success
        assert result.action == 'merged'
        connection = sqlite3.connect(str(tmp_path / 'songs.sqlite'))
        try:
            titles = [row[0] for row in connection.execute('SELECT title FROM songs')]
        finally:
            connection.close()
        assert sorted(titles) == ['Local Song', 'Remote Song']
        assert provider.uploaded  # merged library was uploaded

    def test_merge_failure_falls_back_to_last_write_wins(self, controller, settings, tmp_path,
                                                         monkeypatch):
        (tmp_path / 'songs.sqlite').write_bytes(b'not a database')
        provider = FakeProvider(backups=[make_backup(created_utc='2026-09-29T22:00:00Z',
                                                     sha256='remote-sha')])
        monkeypatch.setattr(controller, '_connect_provider', lambda: provider)

        result = controller.perform_startup_sync()

        # Corrupt local db -> merge raises -> falls back to download path.
        assert result.success
        assert result.action in ('downloaded', 'in-sync')


class TestMergeRefresh:
    def test_successful_merge_refreshes_song_list(self, controller, monkeypatch):
        calls = []
        monkeypatch.setattr(
            'openlp.plugins.cloudsync.lib.synccontroller.refresh_songs_ui',
            lambda: calls.append(True) or True)
        controller.merge_applied = True

        received = []
        controller.sync_finished.connect(lambda ok, msg: received.append((ok, msg)))
        controller._on_worker_finished(True, 'Merged 2 new and 0 updated songs from remote-pc.')

        assert calls == [True]
        assert controller.merge_applied is False  # flag consumed
        assert received[0][0] is True
        assert 'The song list has been refreshed.' in received[0][1]

    def test_failed_refresh_falls_back_to_restart_message(self, controller, monkeypatch):
        monkeypatch.setattr(
            'openlp.plugins.cloudsync.lib.synccontroller.refresh_songs_ui',
            lambda: False)
        controller.merge_applied = True

        received = []
        controller.sync_finished.connect(lambda ok, msg: received.append((ok, msg)))
        controller._on_worker_finished(True, 'Merged 2 new and 0 updated songs from remote-pc.')

        assert controller.merge_applied is False
        assert 'Restart OpenLP to see them in the song list.' in received[0][1]

    def test_no_merge_no_refresh_attempt(self, controller, monkeypatch):
        def _fail():
            raise AssertionError('refresh_songs_ui should not be called')
        monkeypatch.setattr(
            'openlp.plugins.cloudsync.lib.synccontroller.refresh_songs_ui', _fail)

        received = []
        controller.sync_finished.connect(lambda ok, msg: received.append((ok, msg)))
        controller._on_worker_finished(True, 'Library is in sync with the cloud.')

        assert received[0][1] == 'Library is in sync with the cloud.'

    def test_failed_sync_keeps_merge_flag_for_next_success(self, controller, monkeypatch):
        def _fail():
            raise AssertionError('refresh_songs_ui should not be called')
        monkeypatch.setattr(
            'openlp.plugins.cloudsync.lib.synccontroller.refresh_songs_ui', _fail)
        controller.merge_applied = True

        controller._on_worker_finished(False, 'Upload failed: boom')

        assert controller.merge_applied is True
