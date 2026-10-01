"""
Tests for the Cloud Sync OAuth flow (:mod:`auth`).

The module is standard-library only by design (the frozen OpenLP builds do
not ship the Google client libraries): the authorisation URL is built by
hand, the redirect is received by a real local HTTP server, and the code
exchange goes through ``urllib``.

The contract under test:

* ``run_oauth_flow`` opens the system browser with a well-formed Google
  authorisation URL (client id, localhost redirect, offline access, state).
* The redirect is received by the real local server and the OAuth ``state``
  is validated -- a mismatch fails closed.
* A user denial surfaces as :class:`AuthenticationCancelledError`.
* A browser tab that is never completed surfaces as a clear
  :class:`AuthenticationError` ("Timed out"), not an endless
  'Connecting...' hang.
* :class:`OAuthCredentials` round-trips through the token file and
  refreshes expired tokens.
"""
import json
import threading
import time
import urllib.parse
import urllib.request

import pytest

from openlp.plugins.cloudsync.lib import auth as auth_module
from openlp.plugins.cloudsync.lib.providers import GOOGLE_DRIVE_SCOPES


@pytest.fixture
def client_secrets(tmp_path):
    secrets = tmp_path / 'credentials.json'
    secrets.write_text(json.dumps({'installed': {
        'client_id': 'test-client-id',
        'client_secret': 'test-client-secret',
        'auth_uri': 'https://accounts.google.example/o/oauth2/auth',
        'token_uri': 'https://oauth2.google.example/token',
    }}))
    return secrets


@pytest.fixture
def bundled_secrets(client_secrets, monkeypatch):
    """Point the bundled-client lookup at the test secrets file."""
    monkeypatch.setattr(auth_module, 'bundled_client_secrets_path', lambda: str(client_secrets))
    return client_secrets


def make_browser_opener(query_params=None, delay=0.2):
    """
    Build a fake browser: parses the authorisation URL, then (after a short
    delay, on a thread) performs the real HTTP GET a browser would make to
    the redirect URI -- exercising the real local server in run_oauth_flow.
    """
    calls = []

    def opener(auth_url):
        calls.append(auth_url)
        if query_params is None:
            return
        parsed = urllib.parse.urlparse(auth_url)
        params = urllib.parse.parse_qs(parsed.query)
        redirect_uri = params['redirect_uri'][0]
        state = params['state'][0]
        callback = redirect_uri + '?' + urllib.parse.urlencode(
            dict(query_params, state=query_params.get('state', state)))

        def visit():
            time.sleep(delay)
            urllib.request.urlopen(callback, timeout=10).read()

        thread = threading.Thread(target=visit, daemon=True)
        thread.start()

    opener.calls = calls
    return opener


@pytest.fixture
def fake_token_exchange(monkeypatch):
    """Pretend Google's token endpoint; return the canned token response."""
    calls = []

    def exchange(token_uri, client_id, client_secret, code, redirect_uri, timeout=30):
        calls.append({
            'token_uri': token_uri, 'client_id': client_id, 'code': code,
            'redirect_uri': redirect_uri,
        })
        assert code == 'auth-code-123'
        return {
            'access_token': 'access-abc',
            'refresh_token': 'refresh-def',
            'expires_in': 3600,
        }

    monkeypatch.setattr(auth_module, '_exchange_code', exchange)
    return calls


def auth_url_params(opener):
    assert len(opener.calls) == 1
    parsed = urllib.parse.urlparse(opener.calls[0])
    return urllib.parse.parse_qs(parsed.query)


def test_run_oauth_flow_happy_path(bundled_secrets, tmp_path, fake_token_exchange):
    """Browser completes the redirect: token cached, credentials returned."""
    token_path = tmp_path / 'token.json'
    opener = make_browser_opener({'code': 'auth-code-123'})

    credentials = auth_module.run_oauth_flow(
        str(token_path), browser_opener=opener, timeout_seconds=10)

    params = auth_url_params(opener)
    assert params['client_id'] == ['test-client-id']
    assert params['response_type'] == ['code']
    assert params['access_type'] == ['offline']
    assert params['redirect_uri'][0].startswith('http://127.0.0.1:')
    assert 'drive.file' in params['scope'][0]
    assert params['state'][0]  # state present for the round-trip check
    # Code exchange used the token endpoint from the secrets file.
    assert fake_token_exchange[0]['token_uri'] == 'https://oauth2.google.example/token'
    assert fake_token_exchange[0]['client_id'] == 'test-client-id'
    # Credentials cached and returned.
    assert credentials.token == 'access-abc'
    assert credentials.refresh_token == 'refresh-def'
    saved = json.loads(token_path.read_text())
    assert saved['token'] == 'access-abc'
    assert saved['refresh_token'] == 'refresh-def'
    assert saved['client_id'] == 'test-client-id'


def test_run_oauth_flow_state_mismatch_fails_closed(
        bundled_secrets, tmp_path, fake_token_exchange):
    """A redirect with a forged state is rejected, not exchanged."""
    token_path = tmp_path / 'token.json'
    opener = make_browser_opener({'code': 'auth-code-123', 'state': 'forged-state'})

    with pytest.raises(auth_module.AuthenticationError) as excinfo:
        auth_module.run_oauth_flow(
            str(token_path), browser_opener=opener, timeout_seconds=10)

    assert 'state' in str(excinfo.value).lower()
    assert fake_token_exchange == []  # no code exchange attempted
    assert not token_path.exists()


def test_run_oauth_flow_user_denial_is_cancellation(
        bundled_secrets, tmp_path, fake_token_exchange):
    opener = make_browser_opener({'error': 'access_denied'})

    with pytest.raises(auth_module.AuthenticationCancelledError):
        auth_module.run_oauth_flow(
            str(tmp_path / 'token.json'),
            browser_opener=opener, timeout_seconds=10)

    assert fake_token_exchange == []


def test_run_oauth_flow_timeout_becomes_authentication_error(bundled_secrets, tmp_path):
    """A browser tab that is never completed surfaces as a clear error,
    not an endless 'Connecting...' hang."""
    opener = make_browser_opener(query_params=None)  # browser never calls back

    with pytest.raises(auth_module.AuthenticationError) as excinfo:
        auth_module.run_oauth_flow(
            str(tmp_path / 'token.json'),
            browser_opener=opener, timeout_seconds=2)

    assert 'Timed out' in str(excinfo.value)


def test_run_oauth_flow_missing_bundled_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_module, 'bundled_client_secrets_path', lambda: None)

    with pytest.raises(auth_module.AuthenticationError) as excinfo:
        auth_module.run_oauth_flow(
            str(tmp_path / 'token.json'), browser_opener=make_browser_opener())

    assert 'not found' in str(excinfo.value)


def test_run_oauth_flow_malformed_bundled_secrets(tmp_path, monkeypatch):
    bad = tmp_path / 'client.json'
    bad.write_text(json.dumps({'installed': {'client_id': '', 'client_secret': ''}}))
    monkeypatch.setattr(auth_module, 'bundled_client_secrets_path', lambda: str(bad))

    with pytest.raises(auth_module.AuthenticationError) as excinfo:
        auth_module.run_oauth_flow(
            str(tmp_path / 'token.json'), browser_opener=make_browser_opener())

    assert 'client_id' in str(excinfo.value)


def test_credentials_round_trip_and_validity(tmp_path):
    token_path = tmp_path / 'token.json'
    original = auth_module.OAuthCredentials(
        token='access-1', refresh_token='refresh-1',
        token_uri='https://oauth2.google.example/token',
        client_id='cid', client_secret='csecret',
        scopes=GOOGLE_DRIVE_SCOPES, expiry=time.time() + 3600)
    auth_module.save_credentials(original, token_path)

    loaded = auth_module.load_credentials(token_path)
    assert loaded.token == 'access-1'
    assert loaded.refresh_token == 'refresh-1'
    assert loaded.valid
    assert auth_module.has_valid_token(token_path)


def test_load_credentials_refreshes_expired_token(tmp_path, monkeypatch):
    token_path = tmp_path / 'token.json'
    expired = auth_module.OAuthCredentials(
        token='stale', refresh_token='refresh-1',
        token_uri='https://oauth2.google.example/token',
        client_id='cid', client_secret='csecret',
        scopes=GOOGLE_DRIVE_SCOPES, expiry=time.time() - 10)
    auth_module.save_credentials(expired, token_path)

    def fake_post(url, payload, timeout=30):
        assert url == 'https://oauth2.google.example/token'
        return {'access_token': 'fresh-token', 'expires_in': 3600}

    monkeypatch.setattr(auth_module, '_post_form', fake_post)

    loaded = auth_module.load_credentials(token_path)
    assert loaded.token == 'fresh-token'
    assert loaded.valid
    # The refreshed token was persisted.
    assert auth_module.OAuthCredentials.from_authorized_user_file(
        str(token_path)).token == 'fresh-token'


def test_has_valid_token_rejects_garbage(tmp_path):
    token_path = tmp_path / 'token.json'
    token_path.write_text('not json{{{')
    assert not auth_module.has_valid_token(token_path)
    assert auth_module.load_credentials(token_path) is None
    assert not auth_module.has_valid_token(tmp_path / 'missing.json')


def test_revoke_token_reports_whether_anything_was_removed(tmp_path):
    token_path = tmp_path / 'token.json'
    assert auth_module.revoke_token(token_path) is False
    token_path.write_text('{}')
    assert auth_module.revoke_token(token_path) is True
    assert not token_path.exists()


def test_read_client_secrets_reads_bundled(tmp_path, monkeypatch):
    secrets = tmp_path / 'client.json'
    secrets.write_text(json.dumps({'installed': {'client_id': 'bundled-id',
                                                'client_secret': 'bundled-secret'}}),
                       encoding='utf-8')
    monkeypatch.setattr(auth_module, 'bundled_client_secrets_path', lambda: str(secrets))

    client_id, client_secret, _, _ = auth_module._read_client_secrets()

    assert (client_id, client_secret) == ('bundled-id', 'bundled-secret')


def test_read_client_secrets_missing_bundle_raises(monkeypatch):
    monkeypatch.setattr(auth_module, 'bundled_client_secrets_path', lambda: None)

    with pytest.raises(auth_module.AuthenticationError):
        auth_module._read_client_secrets()


# v13.5 requested drive.file + drive.readonly.  v13.6 narrowed back to
# drive.file alone; a token granted the wider set is a superset and keeps
# working -- narrowing must not force a re-consent.
WIDER_SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/drive.readonly',
]
# A token missing drive.file entirely is not sufficient.
NARROWER_SCOPES = [
    'https://www.googleapis.com/auth/drive.metadata.readonly',
]


def _write_token(tmp_path, scopes):
    token_path = tmp_path / 'token.json'
    credentials = auth_module.OAuthCredentials(
        token='access-1', refresh_token='refresh-1',
        token_uri='https://oauth2.google.example/token',
        client_id='cid', client_secret='csecret',
        scopes=scopes, expiry=time.time() + 3600)
    auth_module.save_credentials(credentials, token_path)
    return token_path


def test_has_required_scopes_true_for_current_scopes(tmp_path):
    assert auth_module.has_required_scopes(_write_token(tmp_path, GOOGLE_DRIVE_SCOPES)) is True


def test_has_required_scopes_false_for_older_narrower_scopes(tmp_path):
    # A token granted without drive.file must not be treated as
    # sufficient: API calls would 403.
    assert auth_module.has_required_scopes(_write_token(tmp_path, NARROWER_SCOPES)) is False


def test_has_required_scopes_true_for_wider_superset(tmp_path):
    # Narrowing the requested scopes (v13.5 -> v13.6) must not force a
    # fresh consent: the old token already covers everything needed.
    assert auth_module.has_required_scopes(_write_token(tmp_path, WIDER_SCOPES)) is True


def test_has_required_scopes_false_when_file_missing(tmp_path):
    assert auth_module.has_required_scopes(tmp_path / 'nope.json') is False


def test_missing_file_scope_forces_fresh_consent(tmp_path):
    token_path = _write_token(tmp_path, NARROWER_SCOPES)
    assert auth_module.has_valid_token(token_path) is False
    assert auth_module.load_credentials(token_path) is None


def test_wider_superset_token_still_loads(tmp_path):
    token_path = _write_token(tmp_path, WIDER_SCOPES)
    assert auth_module.has_valid_token(token_path) is True
    assert auth_module.load_credentials(token_path).token == 'access-1'


def test_current_scopes_still_load(tmp_path):
    token_path = _write_token(tmp_path, GOOGLE_DRIVE_SCOPES)
    assert auth_module.has_valid_token(token_path) is True
    assert auth_module.load_credentials(token_path).token == 'access-1'
