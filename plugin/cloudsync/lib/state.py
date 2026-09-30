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
The :mod:`~..state` module persists the sync
engine's view of the world: the fingerprint of the local library the last
time it was synced, and the metadata of the newest remote backup seen.

Comparing this state against the live data directory (local changes) and
the cloud listing (remote changes) is what drives the last-write-wins
decisions in :mod:`~..synccontroller`.
"""
import json
import logging
from pathlib import Path

from .backup import PLUGIN_SECTION_DIR

log = logging.getLogger(__name__)

STATE_FILENAME = 'state.json'


def _state_path(data_dir):
    section = Path(data_dir) / PLUGIN_SECTION_DIR
    section.mkdir(parents=True, exist_ok=True)
    return section / STATE_FILENAME


def load_state(data_dir):
    """
    Load the persisted sync state.

    :param data_dir: The OpenLP data directory.
    :return: Dict with keys ``local_fingerprint``, ``local_synced_utc``,
        ``remote_name``, ``remote_hostname``, ``remote_created_utc``,
        ``remote_sha256`` and ``applied_remote_sha256``.  Missing values
        are None.
    """
    state = {
        'local_fingerprint': None,
        'local_synced_utc': None,
        'remote_name': None,
        'remote_hostname': None,
        'remote_created_utc': None,
        'remote_sha256': None,
        'applied_remote_sha256': None,
    }
    path = _state_path(data_dir)
    if not path.is_file():
        return state
    try:
        loaded = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        log.exception('Could not read cloud sync state at %s', path)
        return state
    state.update({key: loaded.get(key) for key in state})
    return state


def save_state(data_dir, **values):
    """
    Persist sync state values, leaving unmentioned keys untouched.

    :param data_dir: The OpenLP data directory.
    :param values: Key/value pairs to store.
    """
    path = _state_path(data_dir)
    state = load_state(data_dir)
    state.update(values)
    path.write_text(json.dumps(state, indent=2), encoding='utf-8')
    log.debug('Cloud sync state saved: %s', {k: v for k, v in values.items()})


def record_upload(data_dir, fingerprint, metadata):
    """
    Record a successful upload in the sync state.

    :param data_dir: The OpenLP data directory.
    :param str fingerprint: Fingerprint of the local data just uploaded.
    :param dict metadata: Archive metadata of the uploaded backup.
    """
    from datetime import datetime, timezone
    save_state(
        data_dir,
        local_fingerprint=fingerprint,
        local_synced_utc=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        remote_name=metadata.get('name'),
        remote_hostname=metadata.get('hostname'),
        remote_created_utc=metadata.get('created_utc'),
        remote_sha256=metadata.get('sha256'),
        # The remote backup whose content is actually reflected in the
        # local library now.  v12 staged downloads as "seen" without ever
        # applying them, which left machines permanently reporting
        # in-sync; a seen-but-never-applied remote is re-downloaded until
        # it lands (see _is_remote_newer).
        applied_remote_sha256=metadata.get('sha256'),
    )


def record_remote_seen(data_dir, backup):
    """
    Record the newest remote backup seen, without changing local state.

    :param data_dir: The OpenLP data directory.
    :param backup: A :class:`~..providers.RemoteBackup`.
    """
    save_state(
        data_dir,
        remote_name=backup.name,
        remote_hostname=backup.hostname,
        remote_created_utc=backup.created_utc,
        remote_sha256=backup.sha256,
    )
