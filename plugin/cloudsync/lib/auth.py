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
The :mod:`~..auth` module handles OAuth
authentication for cloud sync providers.

The Google Drive flow follows the same "installed app" model as OpenLP
Vault: the plugin carries its own OAuth client (``client.json`` bundled
with the plugin), OpenLP opens the system browser so the user can grant
access, and the resulting token is cached locally and refreshed
automatically; only the ``drive.file`` + ``drive.metadata.readonly``
scopes are requested.

This module is intentionally **standard library only** (``urllib``,
``wsgiref``/``http.server``, ``webbrowser``): the frozen OpenLP builds do
not ship the Google client libraries, and requiring users to ``pip
install`` into a frozen app is not viable.  The OAuth "installed app"
exchange and the Drive REST calls are small enough to implement directly.
"""
import datetime
import json
import logging
import secrets
import socket
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .providers import GOOGLE_DRIVE_SCOPES

log = logging.getLogger(__name__)

DEFAULT_AUTH_URI = 'https://accounts.google.com/o/oauth2/auth'
DEFAULT_TOKEN_URI = 'https://oauth2.googleapis.com/token'


class AuthenticationCancelledError(RuntimeError):
    """
    Raised when the user cancels the OAuth flow.
    """


class AuthenticationError(RuntimeError):
    """
    Raised when authentication fails for a reason other than cancellation.
    """


class OAuthCredentials:
    """
    Minimal OAuth2 credentials: an access token plus everything needed to
    refresh it.  Serialises to the same JSON shape as Google's
    ``authorized_user`` files (``token``, ``refresh_token``, ``token_uri``,
    ``client_id``, ``client_secret``, ``scopes``, ``expiry``).
    """
    def __init__(self, token, refresh_token, token_uri, client_id, client_secret,
                 scopes=None, expiry=None):
        self.token = token
        self.refresh_token = refresh_token
        self.token_uri = token_uri
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = list(scopes or [])
        # ``expiry``: seconds-since-epoch (float) or None when unknown.
        self.expiry = expiry

    @property
    def valid(self):
        """
        True when an access token exists and is not (nearly) expired.
        """
        if not self.token:
            return False
        if self.expiry is None:
            return True
        return self.expiry > time.time() + 60

    def refresh(self):
        """
        Exchange the refresh token for a new access token (blocking HTTP).
        """
        if not self.refresh_token:
            raise AuthenticationError('No refresh token available; reconnect to Google Drive.')
        log.info('Refreshing expired OAuth token')
        payload = urllib.parse.urlencode({
            'grant_type': 'refresh_token',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': self.refresh_token,
        }).encode('ascii')
        data = _post_form(self.token_uri, payload)
        self.token = data['access_token']
        # Google only returns a new refresh token on the first exchange;
        # keep the old one when the response has none.
        self.refresh_token = data.get('refresh_token', self.refresh_token)
        self.expiry = _expiry_from_response(data)

    def to_json(self):
        return json.dumps({
            'token': self.token,
            'refresh_token': self.refresh_token,
            'token_uri': self.token_uri,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'scopes': self.scopes,
            'expiry': _format_expiry(self.expiry),
        })

    @classmethod
    def from_json(cls, payload):
        data = json.loads(payload)
        return cls(
            token=data.get('token'),
            refresh_token=data.get('refresh_token'),
            token_uri=data.get('token_uri', DEFAULT_TOKEN_URI),
            client_id=data.get('client_id'),
            client_secret=data.get('client_secret'),
            scopes=data.get('scopes'),
            expiry=_parse_expiry(data.get('expiry')),
        )

    @classmethod
    def from_authorized_user_file(cls, path, scopes=None):
        with open(path, 'r', encoding='utf-8') as stream:
            credentials = cls.from_json(stream.read())
        if scopes:
            credentials.scopes = list(scopes)
        return credentials


def _format_expiry(expiry):
    if expiry is None:
        return None
    return datetime.datetime.fromtimestamp(expiry, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse_expiry(value):
    if not value:
        return None
    try:
        parsed = datetime.datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
        return parsed.replace(tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _expiry_from_response(data):
    try:
        return time.time() + int(data.get('expires_in', 0))
    except (TypeError, ValueError):
        return None


def _post_form(url, payload, timeout=30):
    """
    POST url-encoded form data, returning the parsed JSON response.

    :raises AuthenticationError: On transport errors or non-2xx responses.
    """
    request = urllib.request.Request(url, data=payload, method='POST',
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode('utf-8', 'replace')
        except Exception:
            detail = ''
        raise AuthenticationError(
            'Google authentication failed (HTTP {status}): {detail}'.format(
                status=error.code, detail=detail[:300])) from error
    except (urllib.error.URLError, socket.timeout, OSError) as error:
        raise AuthenticationError('Could not reach Google: {error}'.format(error=error)) from error


def _stored_scopes(token_path):
    """
    Read the scopes recorded in a cached token file -- i.e. the scopes the
    user actually granted when they consented -- without modifying the file.
    """
    try:
        with open(str(token_path), 'r', encoding='utf-8') as stream:
            return OAuthCredentials.from_json(stream.read()).scopes
    except Exception:
        return []


def has_required_scopes(token_path):
    """
    True when the cached token was granted every scope the plugin
    currently needs.

    A plugin update that widens the requested scopes must send the user
    through the consent screen again; otherwise every API call needing the
    new scope fails with HTTP 403.
    """
    granted = set(_stored_scopes(token_path))
    return all(scope in granted for scope in GOOGLE_DRIVE_SCOPES)


def has_valid_token(token_path):
    """
    Check whether a cached OAuth token exists and is usable (valid or
    refreshable).

    :param token_path: Path to the cached token file.
    :return: True if a usable token exists.
    """
    token_path = Path(token_path)
    if not token_path.is_file():
        return False
    if not has_required_scopes(token_path):
        log.info('Cached token was granted older scopes; fresh consent is required')
        return False
    try:
        credentials = OAuthCredentials.from_authorized_user_file(str(token_path), GOOGLE_DRIVE_SCOPES)
    except Exception:
        log.debug('Could not read cached token at %s', token_path, exc_info=True)
        return False
    return bool(credentials.valid or credentials.refresh_token)


def load_credentials(token_path):
    """
    Load cached OAuth credentials, refreshing them if expired.

    :param token_path: Path to the cached token file.
    :return: An :class:`OAuthCredentials` instance, or None if no usable
        token exists.
    """
    token_path = Path(token_path)
    if not token_path.is_file():
        return None
    if not has_required_scopes(token_path):
        log.info('Cached token was granted older scopes; fresh consent is required')
        return None
    try:
        credentials = OAuthCredentials.from_authorized_user_file(str(token_path), GOOGLE_DRIVE_SCOPES)
    except Exception:
        log.warning('Cached token at %s is unreadable', token_path, exc_info=True)
        return None
    if not credentials.valid:
        if credentials.refresh_token:
            try:
                credentials.refresh()
            except AuthenticationError:
                log.warning('Cached token could not be refreshed', exc_info=True)
                return None
            save_credentials(credentials, token_path)
        else:
            log.warning('Cached token is invalid and cannot be refreshed')
            return None
    return credentials


def save_credentials(credentials, token_path):
    """
    Persist OAuth credentials to disk.

    :param credentials: An :class:`OAuthCredentials` instance.
    :param token_path: Destination path for the token file.
    """
    token_path = Path(token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding='utf-8')
    log.debug('OAuth token saved to %s', token_path)


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """
    Captures the single OAuth redirect, then shows a "done" page.
    """
    # Set by _start_redirect_server before serving: dict-like result store.
    result = None

    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        self.result['params'] = {key: values[0] for key, values in params.items()}
        body = ('<html><body style="font-family:sans-serif">'
                '<h2>OpenLP Cloud Sync is connected.</h2>'
                '<p>You can close this tab and return to OpenLP.</p>'
                '</body></html>').encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        # Keep the local server quiet; failures are logged by the caller.
        pass


def _start_redirect_server():
    """
    Bind the OAuth redirect receiver on an ephemeral loopback port.

    :return: ``(server, port, result)`` where *result* will hold the
        redirect's query parameters under ``'params'`` once received.
    """
    result = {}
    handler = type('_BoundOAuthCallbackHandler', (_OAuthCallbackHandler,), {'result': result})
    server = HTTPServer(('127.0.0.1', 0), handler)
    server.timeout = 1.0
    return server, server.server_address[1], result


def _serve_until_callback(server, result, timeout_seconds):
    """
    Serve the OAuth redirect until the browser calls back or the timeout
    elapses.

    :return: The redirect's query parameters.
    :raises AuthenticationError: On timeout.
    """
    deadline = time.time() + timeout_seconds
    try:
        while time.time() < deadline:
            server.handle_request()
            if result.get('params'):
                return result['params']
    finally:
        server.server_close()
    raise AuthenticationError(
        'Timed out waiting for the Google sign-in to complete in your browser. '
        'Click Connect and complete the sign-in within a few minutes.')


def bundled_client_secrets_path():
    """
    Path of the OAuth client secrets file bundled with the plugin, if any.

    The plugin always uses the ``client.json`` in its own top-level
    directory; there is no user-supplied alternative.
    """
    candidate = Path(__file__).resolve().parent.parent / 'client.json'
    return str(candidate) if candidate.is_file() else None


def _read_client_secrets():
    """
    Parse the bundled Google OAuth "Desktop app" client secrets file.

    :return: ``(client_id, client_secret, auth_uri, token_uri)``.
    :raises AuthenticationError: When the bundled client secrets are missing.
    """
    client_secrets_path = bundled_client_secrets_path()
    client_secrets_path = Path(client_secrets_path) if client_secrets_path else None
    if client_secrets_path is None or not client_secrets_path.is_file():
        raise AuthenticationError(
            'OAuth client secrets not found: the client.json bundled with '
            'the Cloud Sync plugin is missing. Reinstall the plugin.')
    try:
        data = json.loads(client_secrets_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise AuthenticationError(
            'OAuth client secrets file is not valid JSON: {path}'.format(
                path=client_secrets_path)) from error
    section = data.get('installed') or data.get('web') or {}
    client_id = section.get('client_id')
    client_secret = section.get('client_secret')
    if not client_id or not client_secret:
        raise AuthenticationError(
            'OAuth client secrets file is missing client_id/client_secret: {path}'.format(
                path=client_secrets_path))
    return (client_id, client_secret,
            section.get('auth_uri') or DEFAULT_AUTH_URI,
            section.get('token_uri') or DEFAULT_TOKEN_URI)


def _exchange_code(token_uri, client_id, client_secret, code, redirect_uri, timeout=30):
    """
    Exchange an OAuth authorisation code for tokens.

    :return: The parsed token response.
    :raises AuthenticationError: On failure.
    """
    payload = urllib.parse.urlencode({
        'grant_type': 'authorization_code',
        'client_id': client_id,
        'client_secret': client_secret,
        'code': code,
        'redirect_uri': redirect_uri,
    }).encode('ascii')
    data = _post_form(token_uri, payload, timeout=timeout)
    if 'access_token' not in data:
        raise AuthenticationError('Google did not return an access token.')
    return data


def run_oauth_flow(token_path, status_callback=None, timeout_seconds=300,
                   browser_opener=None):
    """
    Run the OAuth "installed app" flow in the calling thread.

    The default web browser is opened to the Google consent page and a
    temporary local web server receives the OAuth redirect.  This function
    blocks until the user completes (or cancels) the flow, or until
    *timeout_seconds* elapses, so callers must run it off the UI thread.

    The OAuth client is always the ``client.json`` bundled with the plugin.

    :param token_path: Where to cache the resulting token.
    :param status_callback: Optional callable receiving status strings.
    :param timeout_seconds: Seconds to wait for the browser sign-in before
        giving up (default 300); without this a missed browser tab hangs
        forever.
    :param browser_opener: Optional callable taking the authorisation URL
        (defaults to :func:`webbrowser.open`); used by tests to simulate the
        browser completing the redirect.
    :return: The obtained :class:`OAuthCredentials`.
    :raises AuthenticationCancelledError: If the user denies access.
    :raises AuthenticationError: If the flow fails or times out.
    """
    client_id, client_secret, auth_uri, token_uri = _read_client_secrets()
    if status_callback is not None:
        status_callback('Waiting for browser authorisation...')
    log.info('Opening default browser for Google OAuth consent')

    state = secrets.token_urlsafe(24)
    # Bind the redirect receiver first so the redirect URI (with its
    # ephemeral loopback port) is known before the browser opens.
    server, port, result = _start_redirect_server()
    redirect_uri = 'http://127.0.0.1:{port}/'.format(port=port)
    auth_url = auth_uri + '?' + urllib.parse.urlencode({
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': ' '.join(GOOGLE_DRIVE_SCOPES),
        'access_type': 'offline',
        'prompt': 'consent',
        'state': state,
    })

    opener = browser_opener or (lambda url: webbrowser.open(url, new=1, autoraise=True))
    try:
        opener(auth_url)
    except Exception as error:
        server.server_close()
        raise AuthenticationError(
            'Could not open the web browser for Google sign-in: {error}'.format(error=error)) from error

    params = _serve_until_callback(server, result, timeout_seconds)
    if params.get('state') != state:
        raise AuthenticationError('OAuth state mismatch; please try connecting again.')
    if params.get('error'):
        if params['error'] == 'access_denied':
            raise AuthenticationCancelledError('Google sign-in was cancelled.')
        raise AuthenticationError(
            'Google sign-in failed: {error}'.format(error=params['error']))
    code = params.get('code')
    if not code:
        raise AuthenticationError('Google did not return an authorisation code.')

    data = _exchange_code(token_uri, client_id, client_secret, code, redirect_uri)
    credentials = OAuthCredentials(
        token=data['access_token'],
        refresh_token=data.get('refresh_token'),
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=GOOGLE_DRIVE_SCOPES,
        expiry=_expiry_from_response(data),
    )
    save_credentials(credentials, token_path)
    log.info('OAuth flow completed successfully')
    return credentials


def revoke_token(token_path):
    """
    Delete the cached OAuth token, disconnecting cloud sync.

    :param token_path: Path to the cached token file.
    :return: True if a token was removed, False if there was none.
    """
    token_path = Path(token_path)
    try:
        token_path.unlink()
        log.info('Removed cached OAuth token at %s', token_path)
        return True
    except FileNotFoundError:
        return False
