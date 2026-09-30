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
The :mod:`~..restore` module tracks staged library downloads.

History: v12 staged downloaded libraries for application at the next
restart, but the released OpenLP app has no hook that runs before plugins
open their databases, so staged restores were never applied.  Since v13,
downloads are applied live (see
:meth:`~..synccontroller.SyncController._apply_downloaded_archive`).
The helpers here exist only so v13 can detect and migrate a leftover v12
staged download on the next sync.
"""
import json
import logging
from pathlib import Path

from .backup import PLUGIN_SECTION_DIR

log = logging.getLogger(__name__)

PENDING_RESTORE_FILENAME = 'pending-restore.json'
STAGED_ARCHIVE_FILENAME = 'staged-library.zip'


def has_pending_restore(data_dir):
    """
    Check whether a staged restore is waiting to be applied.

    :param data_dir: The OpenLP data directory.
    :return: The pending-restore marker dict, or None.
    """
    marker_path = Path(data_dir) / PLUGIN_SECTION_DIR / PENDING_RESTORE_FILENAME
    if not marker_path.is_file():
        return None
    try:
        return json.loads(marker_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        log.exception('Could not read pending restore marker at %s', marker_path)
        return None


def pending_restore_archive_path(data_dir, marker):
    """
    Resolve the staged archive path described by a pending-restore marker.

    :param data_dir: The OpenLP data directory.
    :param dict marker: Marker dict from :func:`has_pending_restore`.
    :return: Path of the staged zip, or None when the marker names nothing.
    """
    name = (marker or {}).get('staged_archive')
    if not name:
        return None
    return Path(data_dir) / PLUGIN_SECTION_DIR / name


def discard_pending_restore(data_dir):
    """
    Drop a staged restore without applying it.

    :param data_dir: The OpenLP data directory.
    """
    section = Path(data_dir) / PLUGIN_SECTION_DIR
    for name in (PENDING_RESTORE_FILENAME, STAGED_ARCHIVE_FILENAME):
        try:
            (section / name).unlink()
        except FileNotFoundError:
            pass
    log.info('Discarded pending library restore')
