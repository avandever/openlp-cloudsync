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
The :mod:`~openlp.plugins.cloudsync.lib.providers` module defines the cloud
storage abstraction used by the sync engine.

Adding a new backend (e.g. Amazon S3, as requested in the OpenLP Vault forum
thread) means subclassing :class:`SyncProvider` and registering it in
:func:`get_provider`.

The Google Drive backend talks to the Drive v3 REST API with the standard
library only (``urllib``) -- no Google client libraries required, so the
plugin works inside the frozen OpenLP builds.
"""
import json
import logging
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

log = logging.getLogger(__name__)

# OAuth scopes used for Google Drive: create/upload files the app owns and
# read metadata for listing.  This is the least-privilege combination that
# supports sync (same scopes as OpenLP Vault).
GOOGLE_DRIVE_SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/drive.readonly',
]


class DriveHttpError(RuntimeError):
    """
    A Google Drive REST API call failed.

    :param status: HTTP status code (or None for transport errors).
    :param detail: Response body or error description.
    """
    def __init__(self, status, detail):
        self.status = status
        self.detail = detail
        super().__init__('Google Drive request failed (HTTP {status}): {detail}'.format(
            status=status, detail=(detail or '')[:300]))


class RemoteBackup(object):
    """
    Metadata describing a single library archive stored in the cloud.
    """
    def __init__(self, remote_id, name, hostname, created_utc, sha256, size_bytes, openlp_version='unknown'):
        self.remote_id = remote_id
        self.name = name
        self.hostname = hostname
        self.created_utc = created_utc
        self.sha256 = sha256
        self.size_bytes = size_bytes
        self.openlp_version = openlp_version

    def __repr__(self):
        return '<RemoteBackup {name} from {host} at {ts}>'.format(
            name=self.name, host=self.hostname, ts=self.created_utc)


class DriveFolder(object):
    """
    A folder in Google Drive, identified by its stable id.
    """
    def __init__(self, folder_id, name):
        self.id = folder_id
        self.name = name

    def __repr__(self):
        return '<DriveFolder {name} ({fid})>'.format(name=self.name, fid=self.id)


class SyncProvider(ABC):
    """
    Abstract cloud storage backend for library archives.
    """
    #: Machine-readable provider id, e.g. 'googledrive'.
    provider_id = 'abstract'
    #: Human-readable name shown in the settings UI.
    display_name = 'Abstract'

    @abstractmethod
    def connect(self, credentials):
        """
        Authenticate and prepare the provider for use.

        :param credentials: Provider-specific credentials object.
        """

    @abstractmethod
    def is_connected(self):
        """
        :return: True if the provider is ready to transfer files.
        """

    @abstractmethod
    def list_backups(self, folder_name):
        """
        List library archives in the sync folder, newest first.

        :param str folder_name: Name of the sync folder in cloud storage.
        :return: List of :class:`RemoteBackup`, newest first.
        """

    @abstractmethod
    def upload_backup(self, archive_path, metadata, folder_name):
        """
        Upload a library archive.

        :param archive_path: Local path of the zip to upload.
        :param dict metadata: Archive metadata (hostname, created_utc, sha256, ...).
        :param str folder_name: Destination folder in cloud storage.
        :return: The :class:`RemoteBackup` describing the uploaded file.
        """

    @abstractmethod
    def download_backup(self, backup, destination_path):
        """
        Download a library archive.

        :param backup: The :class:`RemoteBackup` to download.
        :param destination_path: Local path to write the downloaded zip to.
        """

    @abstractmethod
    def delete_backup(self, backup):
        """
        Delete a library archive from cloud storage.

        :param backup: The :class:`RemoteBackup` to delete.
        """

    @abstractmethod
    def prune_backups(self, folder_name, keep):
        """
        Delete oldest archives, keeping the newest ``keep`` ones.

        :param str folder_name: Sync folder in cloud storage.
        :param int keep: Number of newest archives to keep.
        """


class GoogleDriveProvider(SyncProvider):
    """
    Google Drive backend for cloud sync.

    Uses the same OAuth "installed app" model as OpenLP Vault: the user
    supplies their own ``credentials.json`` from the Google Cloud Console,
    and the obtained token is cached locally.  The app can read every file
    in the Drive account (``drive.readonly``) but only creates, modifies
    and deletes files of its own (``drive.file``): a backup uploaded by
    another tool can be downloaded and merged, never altered or removed.

    Drive v3 is called directly over HTTPS with the standard library --
    the frozen OpenLP builds do not ship the Google client libraries.
    """
    provider_id = 'googledrive'
    display_name = 'Google Drive'

    DRIVE_API = 'https://www.googleapis.com/drive/v3/files'
    UPLOAD_API = 'https://www.googleapis.com/upload/drive/v3/files'

    def __init__(self):
        self._credentials = None
        self._folder_cache = {}

    def connect(self, credentials):
        """
        :param credentials: An :class:`~..auth.OAuthCredentials` instance.
        """
        self._credentials = credentials
        log.info('Connected to Google Drive')

    def is_connected(self):
        return self._credentials is not None

    def _require_credentials(self):
        if self._credentials is None:
            raise RuntimeError('Google Drive provider is not connected')

    def _request(self, method, url, params=None, json_body=None, raw_body=None,
                 content_type=None, extra_headers=None, timeout=120):
        """
        Perform one authorised Drive API call, refreshing the token once on
        a 401 and returning the parsed JSON response (or raw bytes for
        downloads).

        :raises DriveHttpError: On API or transport failures.
        """
        self._require_credentials()
        if not self._credentials.valid:
            self._credentials.refresh()
        if params:
            url = url + '?' + urllib.parse.urlencode(params)
        attempt = 0
        while True:
            attempt += 1
            headers = {'Authorization': 'Bearer {token}'.format(token=self._credentials.token)}
            if extra_headers:
                headers.update(extra_headers)
            body = None
            if json_body is not None:
                body = json.dumps(json_body).encode('utf-8')
                headers['Content-Type'] = 'application/json; charset=utf-8'
            elif raw_body is not None:
                body = raw_body
                if content_type:
                    headers['Content-Type'] = content_type
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = response.read()
                    response_headers = dict(response.headers)
                    content_kind = response.headers.get_content_type()
            except urllib.error.HTTPError as error:
                if error.code == 401 and attempt == 1 and self._credentials.refresh_token:
                    log.info('Drive token expired, refreshing and retrying')
                    self._credentials.refresh()
                    continue
                try:
                    detail = error.read().decode('utf-8', 'replace')
                except Exception:
                    detail = ''
                if error.code == 403:
                    # The common cause after a plugin update is a token
                    # granted under older, narrower scopes: tell the user
                    # the fix instead of dumping raw JSON at them.
                    detail = ('Permission denied. If the plugin was recently updated, sign out '
                              'of Google Drive in the plugin settings and sign back in to grant '
                              'the new permission. Details: ' + detail)
                raise DriveHttpError(error.code, detail) from error
            except (urllib.error.URLError, OSError) as error:
                raise DriveHttpError(None, str(error)) from error
            if content_kind == 'application/json':
                return json.loads(payload.decode('utf-8')), response_headers
            return payload, response_headers

    def _resolve_folder_id(self, folder_ref):
        """
        Turn a folder reference into a Drive folder id.

        A reference is either ``'id:<folder-id>'`` (stored by the folder
        picker; used directly so renames cannot break it) or a plain folder
        name (looked up, creating it when missing).

        :param str folder_ref: Folder reference, e.g. ``'id:abc123'`` or
            ``'OpenLP'``.
        :return: The Drive folder id.
        """
        self._require_credentials()
        if folder_ref in self._folder_cache:
            return self._folder_cache[folder_ref]
        if folder_ref.startswith('id:'):
            folder_id = folder_ref[3:]
            self._folder_cache[folder_ref] = folder_id
            return folder_id
        query = ("mimeType = 'application/vnd.google-apps.folder' and "
                 "name = '{name}' and trashed = false".format(name=folder_ref.replace("'", "\\'")))
        response, _ = self._request('GET', self.DRIVE_API, params={
            'q': query, 'spaces': 'drive', 'fields': 'files(id, name)'})
        files = response.get('files', [])
        if files:
            folder_id = files[0]['id']
        else:
            log.info('Creating Google Drive folder "%s"', folder_ref)
            folder_id = self.create_folder(folder_ref).id
        self._folder_cache[folder_ref] = folder_id
        return folder_id

    def _list_folders_by_query(self, query):
        """
        Run a folder ``files.list`` query, following every page.

        :param str query: The Drive ``q`` parameter.
        :return: List of :class:`DriveFolder`, sorted by name.
        """
        folders = []
        page_token = None
        while True:
            params = {
                'q': query,
                'spaces': 'drive',
                'fields': 'nextPageToken, files(id, name)',
                'orderBy': 'name',
            }
            if page_token:
                params['pageToken'] = page_token
            response, _ = self._request('GET', self.DRIVE_API, params=params)
            for resource in response.get('files', []):
                folders.append(DriveFolder(resource['id'], resource.get('name', '')))
            page_token = response.get('nextPageToken')
            if not page_token:
                break
        return folders

    def list_folders(self, parent_id=None):
        """
        List the direct sub-folders of a Drive folder.

        :param parent_id: Drive folder id, or None for "My Drive" root.
        :return: List of :class:`DriveFolder`, sorted by name.
        """
        self._require_credentials()
        parent = "'root'" if parent_id is None else "'{pid}'".format(pid=parent_id)
        query = ("mimeType = 'application/vnd.google-apps.folder' and "
                 "{parent} in parents and trashed = false".format(parent=parent))
        return self._list_folders_by_query(query)

    def list_shared_folders(self):
        """
        List top-level folders shared with the user ("Shared with me").

        Under the ``drive.file`` scope this only returns folders the app
        itself created (e.g. a sync folder one machine made and shared with
        another account) -- folders shared from outside the app are invisible
        to it by design.

        :return: List of :class:`DriveFolder`, sorted by name.
        """
        self._require_credentials()
        query = ("mimeType = 'application/vnd.google-apps.folder' and "
                 "sharedWithMe = true and trashed = false")
        return self._list_folders_by_query(query)

    def create_folder(self, name, parent_id=None):
        """
        Create a folder in Drive.

        :param str name: Folder name.
        :param parent_id: Parent Drive folder id, or None for "My Drive" root.
        :return: The new :class:`DriveFolder`.
        """
        self._require_credentials()
        body = {'name': name, 'mimeType': 'application/vnd.google-apps.folder'}
        if parent_id is not None:
            body['parents'] = [parent_id]
        response, _ = self._request('POST', self.DRIVE_API, json_body=body,
                                    params={'fields': 'id, name'})
        log.info('Created Google Drive folder "%s"', name)
        return DriveFolder(response['id'], response.get('name', name))

    @staticmethod
    def _to_backup(file_resource):
        """
        Convert a Drive file resource into a :class:`RemoteBackup`, reading
        sync metadata from ``appProperties``.

        ``appProperties`` are only visible to the OAuth client that created
        the file, so when another client uploaded the archive (e.g. a
        command-line recovery upload) the plugin falls back to the Drive
        file's own ``createdTime`` for ordering.
        """
        props = file_resource.get('appProperties', {}) or {}
        return RemoteBackup(
            remote_id=file_resource['id'],
            name=file_resource.get('name', ''),
            hostname=props.get('hostname', 'unknown'),
            created_utc=props.get('created_utc') or file_resource.get('createdTime', ''),
            sha256=props.get('sha256', ''),
            size_bytes=int(file_resource.get('size', 0) or 0),
            openlp_version=props.get('openlp_version', 'unknown'),
        )

    @staticmethod
    def _is_library_archive(name):
        return name.startswith('openlp-library-') and name.endswith('.zip')

    def list_backups(self, folder_name):
        self._require_credentials()
        folder_id = self._resolve_folder_id(folder_name)
        query = "'{fid}' in parents and trashed = false".format(fid=folder_id)
        backups = []
        page_token = None
        while True:
            params = {
                'q': query,
                'spaces': 'drive',
                'fields': 'nextPageToken, files(id, name, size, appProperties, createdTime)',
            }
            if page_token:
                params['pageToken'] = page_token
            response, _ = self._request('GET', self.DRIVE_API, params=params)
            for resource in response.get('files', []):
                if self._is_library_archive(resource.get('name', '')):
                    backups.append(self._to_backup(resource))
            page_token = response.get('nextPageToken')
            if not page_token:
                break
        # Newest first; fall back to name ordering for archives without metadata.
        backups.sort(key=lambda b: (b.created_utc, b.name), reverse=True)
        log.debug('Found %d library archives in Drive folder "%s"', len(backups), folder_name)
        return backups

    def upload_backup(self, archive_path, metadata, folder_name):
        self._require_credentials()
        folder_id = self._resolve_folder_id(folder_name)
        archive_path = Path(archive_path)
        log.info('Uploading %s to Google Drive folder "%s"', metadata['name'], folder_name)
        # Resumable upload: start a session, then PUT the bytes.  Handles
        # multi-hundred-megabyte libraries better than a single multipart POST.
        session_body = {
            'name': metadata['name'],
            'parents': [folder_id],
            'appProperties': {
                'hostname': metadata.get('hostname', ''),
                'created_utc': metadata.get('created_utc', ''),
                'sha256': metadata.get('sha256', ''),
                'openlp_version': metadata.get('openlp_version', 'unknown'),
            },
        }
        size = archive_path.stat().st_size
        _, headers = self._request('POST', self.UPLOAD_API,
                                   params={'uploadType': 'resumable'},
                                   json_body=session_body,
                                   extra_headers={
                                       'X-Upload-Content-Type': 'application/zip',
                                       'X-Upload-Content-Length': str(size),
                                   })
        session_uri = headers.get('Location')
        if not session_uri:
            raise DriveHttpError(None, 'Drive did not return an upload session URI')
        with open(archive_path, 'rb') as stream:
            content = stream.read()
        resource, _ = self._request('PUT', session_uri, raw_body=content,
                                    content_type='application/zip')
        backup = self._to_backup(resource)
        log.info('Uploaded %s (%d bytes)', backup.name, backup.size_bytes)
        return backup

    def download_backup(self, backup, destination_path):
        self._require_credentials()
        destination_path = Path(destination_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        url = '{api}/{file_id}'.format(api=self.DRIVE_API, file_id=backup.remote_id)
        payload, _ = self._request('GET', url, params={'alt': 'media'}, timeout=600)
        with open(destination_path, 'wb') as stream:
            stream.write(payload)
        log.info('Downloaded %s (%d bytes) to %s', backup.name, destination_path.stat().st_size,
                 destination_path)
        return destination_path

    def delete_backup(self, backup):
        self._require_credentials()
        url = '{api}/{file_id}'.format(api=self.DRIVE_API, file_id=backup.remote_id)
        self._request('DELETE', url)
        log.info('Deleted remote backup %s', backup.name)

    def prune_backups(self, folder_name, keep):
        backups = self.list_backups(folder_name)
        for old_backup in backups[keep:]:
            try:
                self.delete_backup(old_backup)
            except DriveHttpError:
                # The app can only delete files it created itself: a backup
                # uploaded by another tool (e.g. a manual recovery seed) is
                # left alone instead of failing the sync.
                log.warning('Could not delete remote backup %s; leaving it in place',
                            old_backup.name, exc_info=True)


def get_provider(provider_id):
    """
    Instantiate a sync provider by id.

    :param str provider_id: e.g. 'googledrive'.
    :return: A :class:`SyncProvider` instance.
    """
    providers = {
        GoogleDriveProvider.provider_id: GoogleDriveProvider,
    }
    try:
        return providers[provider_id]()
    except KeyError:
        raise ValueError('Unknown cloud sync provider: {pid}'.format(pid=provider_id))
