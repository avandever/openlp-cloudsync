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
The :mod:`.` package holds the cloud sync engine.

Snapshot and archive logic lives in :mod:`~..backup`.
self-describing snapshots of the OpenLP data directory for cloud sync.

OpenLP keeps its SQLite databases open while it runs, so copying the raw
files can produce a corrupt backup.  Every ``*.sqlite``/``*.db`` file is
therefore snapshotted with SQLite's ``VACUUM INTO`` (a transactionally
consistent copy) before being zipped.  The plugin's own working files
(tokens, sync state, staged downloads) are excluded from the archive.
"""
from .backup import (
    PLUGIN_SECTION_DIR,
    compute_fingerprint,
    compute_sha256,
    create_library_archive,
)

__all__ = [
    "PLUGIN_SECTION_DIR",
    "compute_fingerprint",
    "compute_sha256",
    "create_library_archive",
]
