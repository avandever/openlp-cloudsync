"""
Tests for the stdlib-only :class:`GoogleDriveProvider`.

``urllib.request.urlopen`` is faked at the transport level: every test
scripts the HTTP responses Drive would return and asserts on the requests
the provider makes (method, URL, query, JSON body).  No network, no Google
client libraries.
"""
import io
import json
import time
import urllib.error
import urllib.parse

import pytest

from openlp.plugins.cloudsync.lib import auth as auth_module
from openlp.plugins.cloudsync.lib.providers import (
    DriveHttpError, GoogleDriveProvider, RemoteBackup, get_provider,
)


class FakeHeaders(dict):
    def get_content_type(self):
        return self.get('Content-Type', 'application/json').split(';')[0]


class FakeResponse:
    def __init__(self, body, headers=None, content_type='application/json'):
        if isinstance(body, dict):
            body = json.dumps(body).encode('utf-8')
        self._body = io.BytesIO(body)
        self.headers = FakeHeaders(headers or {})
        self.headers.setdefault('Content-Type', content_type)

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeTransport:
    """
    Stand-in for ``urllib.request.urlopen``.  ``script`` is a list of
    ``(response_or_error, expected)`` tuples consumed in order; every request
    is recorded in ``requests``.
    """
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, request, timeout=None):
        # Streamed bodies are file objects consumed (and closed) by the
        # caller: snapshot them now, as the real transport would send them.
        request.sent_body = request.data.read() if hasattr(request.data, 'read') else request.data
        self.requests.append(request)
        if not self.script:
            raise AssertionError('Unexpected request: {m} {u}'.format(
                m=request.get_method(), u=request.full_url))
        outcome, _ = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def last_request_parts(self):
        request = self.requests[-1]
        parsed = urllib.parse.urlparse(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        body = request.data
        json_body = json.loads(body.decode('utf-8')) if body and \
            request.headers.get('Content-type', '').startswith('application/json') else None
        return request.get_method(), parsed.path, query, json_body, dict(request.headers)


def make_credentials(**overrides):
    values = {
        'token': 'access-token',
        'refresh_token': 'refresh-token',
        'token_uri': 'https://oauth2.google.example/token',
        'client_id': 'cid',
        'client_secret': 'csecret',
        'scopes': [],
        'expiry': time.time() + 3600,
    }
    values.update(overrides)
    return auth_module.OAuthCredentials(**values)


@pytest.fixture
def provider():
    instance = GoogleDriveProvider()
    instance.connect(make_credentials())
    return instance


def drive_file(file_id='file-1', name='openlp-library-test.zip', **props):
    resource = {'id': file_id, 'name': name, 'size': '42',
                'appProperties': {'hostname': 'pc', 'created_utc': '2026-09-29T20:00:00Z',
                                  'sha256': 'abc', 'openlp_version': '3.1.3'}}
    resource['appProperties'].update(props)
    return resource


def test_get_provider_returns_drive_provider():
    assert isinstance(get_provider('googledrive'), GoogleDriveProvider)
    with pytest.raises(ValueError):
        get_provider('dropbox')


def test_to_backup_prefers_app_properties_created_utc():
    resource = drive_file('f-1', created_utc='2026-09-29T20:00:00Z')
    resource['createdTime'] = '2026-09-29T21:00:00.000Z'

    backup = GoogleDriveProvider._to_backup(resource)

    assert backup.created_utc == '2026-09-29T20:00:00Z'


def test_to_backup_falls_back_to_drive_created_time():
    # appProperties are only visible to the OAuth client that uploaded the
    # file, so a CLI recovery upload has none: use Drive's createdTime.
    resource = drive_file('f-1')
    del resource['appProperties']['created_utc']
    resource['createdTime'] = '2026-09-29T20:00:00.123Z'

    backup = GoogleDriveProvider._to_backup(resource)

    assert backup.created_utc == '2026-09-29T20:00:00.123Z'


def test_to_backup_empty_when_no_timestamps():
    resource = drive_file('f-1')
    del resource['appProperties']['created_utc']

    backup = GoogleDriveProvider._to_backup(resource)

    assert backup.created_utc == ''


def test_list_backups_paginates_and_sorts_newest_first(provider, monkeypatch):
    transport = FakeTransport([
        # Folder lookup.
        (FakeResponse({'files': [{'id': 'folder-9', 'name': 'OpenLP'}]}), None),
        # Page 1 of files.
        (FakeResponse({'nextPageToken': 'tok',
                       'files': [drive_file('f-old', created_utc='2026-09-28T20:00:00Z'),
                                 {'id': 'not-zip', 'name': 'notes.txt', 'size': '1'}]}), None),
        # Page 2 of files.
        (FakeResponse({'files': [drive_file('f-new', created_utc='2026-09-29T20:00:00Z')]}), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    backups = provider.list_backups('OpenLP')

    assert [b.remote_id for b in backups] == ['f-new', 'f-old']
    assert backups[0].hostname == 'pc'
    assert backups[0].size_bytes == 42
    # The file listing was scoped to the folder with a page token on page 2.
    method, path, query, _, _ = transport.last_request_parts()
    assert method == 'GET' and path == '/drive/v3/files'
    assert query['pageToken'] == ['tok']
    assert "'folder-9' in parents" in query['q'][0]


def test_get_folder_id_creates_missing_folder(provider, monkeypatch):
    transport = FakeTransport([
        (FakeResponse({'files': []}), None),
        (FakeResponse({'id': 'folder-new'}), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    folder_id = provider._resolve_folder_id('OpenLP')

    assert folder_id == 'folder-new'
    method, path, _, json_body, _ = transport.last_request_parts()
    assert method == 'POST' and path == '/drive/v3/files'
    assert json_body['mimeType'] == 'application/vnd.google-apps.folder'
    assert json_body['name'] == 'OpenLP'
    # Cached: no further requests.
    assert provider._resolve_folder_id('OpenLP') == 'folder-new'
    assert len(transport.requests) == 2


def test_upload_backup_uses_resumable_session(provider, monkeypatch, tmp_path):
    archive = tmp_path / 'openlp-library-x.zip'
    archive.write_bytes(b'ZIP' * 100)
    transport = FakeTransport([
        (FakeResponse({'files': [{'id': 'folder-9', 'name': 'OpenLP'}]}), None),
        # Session initiation returns the upload URI in the Location header.
        (FakeResponse({}, headers={'Location': 'https://upload.example/session-1'}), None),
        # Final PUT returns the file resource.
        (FakeResponse(drive_file('f-up', name='openlp-library-x.zip', sha256='deadbeef')), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    backup = provider.upload_backup(
        archive,
        {'name': 'openlp-library-x.zip', 'hostname': 'pc',
         'created_utc': '2026-09-29T21:00:00Z', 'sha256': 'deadbeef',
         'openlp_version': '3.1.3'},
        'OpenLP')

    assert isinstance(backup, RemoteBackup)
    assert backup.remote_id == 'f-up'
    assert backup.sha256 == 'deadbeef'
    # Session initiation: resumable upload type + metadata body.
    session_request = transport.requests[1]
    assert 'uploadType=resumable' in session_request.full_url
    session_body = json.loads(session_request.data.decode('utf-8'))
    assert session_body['name'] == 'openlp-library-x.zip'
    assert session_body['parents'] == ['folder-9']
    assert session_body['appProperties']['hostname'] == 'pc'
    # Byte upload went to the session URI.
    put_request = transport.requests[2]
    assert put_request.get_method() == 'PUT'
    assert put_request.full_url == 'https://upload.example/session-1'
    # ...streamed from the file (not read into memory) with an explicit length.
    assert hasattr(put_request.data, 'read')
    assert put_request.sent_body == b'ZIP' * 100
    assert put_request.headers['Content-length'] == '300'


def test_streamed_upload_rewinds_on_token_refresh_retry(provider, monkeypatch, tmp_path):
    archive = tmp_path / 'openlp-library-x.zip'
    archive.write_bytes(b'ZIP' * 100)
    monkeypatch.setattr(provider._credentials, 'refresh', lambda: None)
    transport = FakeTransport([
        (FakeResponse({'files': [{'id': 'folder-9', 'name': 'OpenLP'}]}), None),
        (FakeResponse({}, headers={'Location': 'https://upload.example/session-1'}), None),
        (urllib.error.HTTPError('https://upload.example/session-1', 401, 'expired', {},
                                io.BytesIO(b'')), None),
        (FakeResponse(drive_file('f-up', name='openlp-library-x.zip')), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    provider.upload_backup(archive, {'name': 'openlp-library-x.zip'}, 'OpenLP')

    assert transport.requests[2].sent_body == b'ZIP' * 100
    assert transport.requests[3].sent_body == b'ZIP' * 100


def test_download_backup_writes_bytes(provider, monkeypatch, tmp_path):
    transport = FakeTransport([
        (FakeResponse(b'ZIPBYTES', content_type='application/zip'), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)
    destination = tmp_path / 'downloads' / 'openlp-library-x.zip'

    provider.download_backup(RemoteBackup('f-1', 'openlp-library-x.zip', 'pc',
                                          '2026-09-29T21:00:00Z', 'abc', 8), destination)

    assert destination.read_bytes() == b'ZIPBYTES'
    method, path, query, _, _ = transport.last_request_parts()
    assert method == 'GET' and path == '/drive/v3/files/f-1'
    assert query['alt'] == ['media']


def test_delete_backup(provider, monkeypatch):
    transport = FakeTransport([(FakeResponse({}), None)])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    provider.delete_backup(RemoteBackup('f-1', 'x.zip', 'pc', '', '', 0))

    method, path, _, _, _ = transport.last_request_parts()
    assert method == 'DELETE' and path == '/drive/v3/files/f-1'


def test_unauthorized_refreshes_token_and_retries(provider, monkeypatch):
    credentials = provider._credentials
    refreshed = []

    def fake_refresh():
        refreshed.append(True)
        credentials.token = 'new-access-token'

    credentials.refresh = fake_refresh
    error = urllib.error.HTTPError('https://www.googleapis.com/drive/v3/files', 401,
                                   'Unauthorized', FakeHeaders(), io.BytesIO(b'{}'))
    transport = FakeTransport([
        (FakeResponse({'files': [{'id': 'folder-9', 'name': 'OpenLP'}]}), None),
        (error, None),
        (FakeResponse({'files': []}), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    backups = provider.list_backups('OpenLP')

    assert backups == []
    assert refreshed == [True]
    # The retried request carried the refreshed token.
    assert transport.requests[-1].headers['Authorization'] == 'Bearer new-access-token'


def test_api_error_becomes_drive_http_error(provider, monkeypatch):
    error = urllib.error.HTTPError('https://www.googleapis.com/drive/v3/files', 403,
                                   'Forbidden', FakeHeaders(),
                                   io.BytesIO(b'{"error": "forbidden"}'))
    transport = FakeTransport([(error, None)])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    with pytest.raises(DriveHttpError) as excinfo:
        provider._resolve_folder_id('OpenLP')

    assert excinfo.value.status == 403


def test_list_folders_root_queries_root_parents(provider, monkeypatch):
    transport = FakeTransport([
        (FakeResponse({'files': [{'id': 'f1', 'name': 'Songs'}, {'id': 'f2', 'name': 'Bibles'}]}), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    folders = provider.list_folders()

    method, path, query, _, _ = transport.last_request_parts()
    assert method == 'GET'
    assert "'root' in parents" in query['q'][0]
    assert "mimeType = 'application/vnd.google-apps.folder'" in query['q'][0]
    assert [(f.id, f.name) for f in folders] == [('f1', 'Songs'), ('f2', 'Bibles')]


def test_list_folders_child_queries_parent_id(provider, monkeypatch):
    transport = FakeTransport([
        (FakeResponse({'files': [{'id': 'f3', 'name': 'Choir'}]}), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    folders = provider.list_folders('parent-1')

    _, _, query, _, _ = transport.last_request_parts()
    assert "'parent-1' in parents" in query['q'][0]
    assert [f.id for f in folders] == ['f3']


def test_create_folder_without_parent(provider, monkeypatch):
    transport = FakeTransport([
        (FakeResponse({'id': 'new-1', 'name': 'Hymns'}), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    folder = provider.create_folder('Hymns')

    method, _, _, json_body, _ = transport.last_request_parts()
    assert method == 'POST'
    assert json_body['name'] == 'Hymns'
    assert json_body['mimeType'] == 'application/vnd.google-apps.folder'
    assert 'parents' not in json_body
    assert folder.id == 'new-1'


def test_create_folder_with_parent(provider, monkeypatch):
    transport = FakeTransport([
        (FakeResponse({'id': 'new-2', 'name': 'Youth'}), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    provider.create_folder('Youth', 'parent-7')

    _, _, _, json_body, _ = transport.last_request_parts()
    assert json_body['parents'] == ['parent-7']


def test_resolve_folder_id_prefix_skips_lookup(provider, monkeypatch):
    transport = FakeTransport([])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    assert provider._resolve_folder_id('id:pinned-42') == 'pinned-42'
    assert transport.requests == []


def test_resolve_folder_id_creates_missing_folder(provider, monkeypatch):
    transport = FakeTransport([
        (FakeResponse({'files': []}), None),
        (FakeResponse({'id': 'made-1', 'name': 'NewName'}), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    assert provider._resolve_folder_id('NewName') == 'made-1'
    # Second call is served from cache: no new requests.
    assert provider._resolve_folder_id('NewName') == 'made-1'
    assert len(transport.requests) == 2


def test_not_connected_raises():
    provider = GoogleDriveProvider()
    assert not provider.is_connected()
    with pytest.raises(RuntimeError):
        provider.list_backups('OpenLP')


def test_list_shared_folders_queries_shared_with_me(provider, monkeypatch):
    transport = FakeTransport([
        (FakeResponse({'files': [{'id': 's1', 'name': 'Choir Songs'},
                                 {'id': 's2', 'name': 'Shared Library'}]}), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    folders = provider.list_shared_folders()

    method, _, query, _, _ = transport.last_request_parts()
    assert method == 'GET'
    assert 'sharedWithMe = true' in query['q'][0]
    assert "mimeType = 'application/vnd.google-apps.folder'" in query['q'][0]
    assert 'in parents' not in query['q'][0]
    assert [(f.id, f.name) for f in folders] == [('s1', 'Choir Songs'), ('s2', 'Shared Library')]


def test_list_shared_folders_paginates(provider, monkeypatch):
    transport = FakeTransport([
        (FakeResponse({'nextPageToken': 'tok',
                       'files': [{'id': 's1', 'name': 'One'}]}), None),
        (FakeResponse({'files': [{'id': 's2', 'name': 'Two'}]}), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    folders = provider.list_shared_folders()

    assert [f.id for f in folders] == ['s1', 's2']
    assert len(transport.requests) == 2


def test_list_shared_folders_requires_connection():
    provider = GoogleDriveProvider()
    with pytest.raises(RuntimeError):
        provider.list_shared_folders()


def test_drive_scopes_are_file_only():
    # drive.file alone: the app sees only files it created or the user
    # opened with it.  Both sync machines share one OAuth client, so every
    # plugin backup counts as app-created.  Keeping the restricted
    # drive.readonly scope out is what keeps Google verification free.
    from openlp.plugins.cloudsync.lib.providers import GOOGLE_DRIVE_SCOPES
    assert GOOGLE_DRIVE_SCOPES == ['https://www.googleapis.com/auth/drive.file']


def _http_error(code):
    import io
    import urllib.error
    return urllib.error.HTTPError('https://www.googleapis.com/drive/v3/files/x', code,
                                  'Forbidden', {}, io.BytesIO(b'{"error": {"code": 403}}'))


def test_prune_backups_leaves_foreign_files_alone(provider, monkeypatch):
    # The app cannot delete a backup uploaded by another tool (drive.file
    # scope): pruning must log and continue, not fail the sync.
    transport = FakeTransport([
        (FakeResponse({'files': [{'id': 'folder-9', 'name': 'OpenLP'}]}), None),
        (FakeResponse({'files': [drive_file('f-new', created_utc='2026-09-29T20:00:00Z'),
                                 drive_file('f-old', created_utc='2026-09-28T20:00:00Z')]}), None),
        (_http_error(403), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    provider.prune_backups('OpenLP', keep=1)  # must not raise

    deletes = [r for r in transport.requests if r.get_method() == 'DELETE']
    assert len(deletes) == 1
    assert 'f-old' in deletes[0].full_url


def test_403_error_points_at_reconsent(provider, monkeypatch):
    transport = FakeTransport([
        (FakeResponse({'files': [{'id': 'folder-9', 'name': 'OpenLP'}]}), None),
        (_http_error(403), None),
    ])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    from openlp.plugins.cloudsync.lib.providers import DriveHttpError
    with pytest.raises(DriveHttpError) as excinfo:
        provider.list_backups('OpenLP')
    assert 'sign back in' in str(excinfo.value)


def test_download_backup_streams_large_body_in_chunks(provider, monkeypatch, tmp_path):
    from openlp.plugins.cloudsync.lib import providers as providers_module
    body = bytes(range(256)) * 50
    response = FakeResponse(body, content_type='application/zip')
    reads = []
    original_read = response.read

    def tracking_read(size=-1):
        reads.append(size)
        return original_read(size)

    response.read = tracking_read
    monkeypatch.setattr(providers_module, 'DOWNLOAD_CHUNK_SIZE', 1000)
    monkeypatch.setattr('urllib.request.urlopen', FakeTransport([(response, None)]))
    destination = tmp_path / 'openlp-library-x.zip'

    provider.download_backup(RemoteBackup('f-1', 'openlp-library-x.zip', 'pc',
                                          '2026-09-29T21:00:00Z', 'abc', len(body)), destination)

    assert destination.read_bytes() == body
    assert reads and all(size == 1000 for size in reads)


def test_folder_name_query_escapes_backslash_and_quote(provider, monkeypatch):
    transport = FakeTransport([(FakeResponse({'files': [{'id': 'f-1', 'name': 'x'}]}), None)])
    monkeypatch.setattr('urllib.request.urlopen', transport)

    provider._resolve_folder_id(r"Bob's \ folder")

    _, _, query, _, _ = transport.last_request_parts()
    assert r"name = 'Bob\'s \\ folder'" in query['q'][0]
