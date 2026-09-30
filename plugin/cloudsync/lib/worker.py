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
The :mod:`~..worker` module provides
:class:`~openlp.core.threading.ThreadWorker` subclasses that run cloud
sync operations off the UI thread.

Workers never touch Qt widgets directly.  They report progress through
the ``status_message`` signal and completion through ``finished``; the
sync controller wires those to the GUI thread with queued connections
(see :func:`openlp.core.threading.run_thread`).
"""
import logging
import shutil
import tempfile
import traceback
from pathlib import Path

from .qtcompat import QtCore

from openlp.core.threading import ThreadWorker

log = logging.getLogger(__name__)


class SyncWorker(ThreadWorker):
    """
    Base worker for cloud sync operations.

    :param controller: The owning
        :class:`~..synccontroller.SyncController`.
    """
    status_message = QtCore.Signal(str)
    finished = QtCore.Signal(bool, str)

    def __init__(self, controller):
        super().__init__()
        self.controller = controller

    def run(self):
        """
        Execute the sync operation.  Must be overridden; any exception is
        caught and reported through ``finished``.
        """
        raise NotImplementedError

    def start(self):
        """
        Thread entry point: run the operation and always emit ``quit`` so
        the thread is torn down.
        """
        try:
            self.run()
        except Exception as error:
            log.exception('Cloud sync worker failed')
            self.finished.emit(False, '{error}\n{trace}'.format(
                error=error, trace=traceback.format_exc(limit=5)))
        finally:
            self.quit.emit()


class StartupSyncWorker(SyncWorker):
    """
    Worker performing the automatic sync that runs when OpenLP starts.
    """
    def run(self):
        self.status_message.emit('Checking cloud library...')
        result = self.controller.perform_startup_sync(status_callback=self.status_message.emit)
        self.finished.emit(result.success, result.message)


class SongChangeSyncWorker(SyncWorker):
    """
    Worker uploading the library after songs were created or updated.
    """
    def run(self):
        self.status_message.emit('Syncing library to cloud...')
        result = self.controller.perform_upload(status_callback=self.status_message.emit)
        self.finished.emit(result.success, result.message)


class ManualSyncWorker(SyncWorker):
    """
    Worker for a user-triggered "Sync now" (upload-first, then pull check).
    """
    def run(self):
        self.status_message.emit('Syncing library with cloud...')
        result = self.controller.perform_manual_sync(status_callback=self.status_message.emit)
        self.finished.emit(result.success, result.message)


class AuthWorker(SyncWorker):
    """
    Worker running the OAuth flow (blocks on the browser redirect).
    """
    authenticated = QtCore.Signal(object)

    def run(self):
        from . import auth as auth_module
        self.status_message.emit('Waiting for browser authorisation...')
        credentials = self.controller.perform_authentication()
        self.authenticated.emit(credentials)
        self.finished.emit(True, 'Connected.')
