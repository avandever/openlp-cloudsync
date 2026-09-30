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
# but WITHOUT ANY WARRANTY; without even the implied warranty of        #
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the          #
# GNU General Public License for more details.                           #
#                                                                        #
# You should have received a copy of the GNU General Public License      #
# along with this program.  If not, see <https://www.gnu.org/licenses/>. #
##########################################################################
"""
The :mod:`~..cloudsynctab` module provides the
settings tab for the Cloud Sync plugin.
"""
import logging

from .qtcompat import QtCore, QtWidgets

from openlp.core.common.i18n import translate
from openlp.core.common.registry import Registry
from openlp.core.lib.settingstab import SettingsTab

log = logging.getLogger(__name__)


class DriveFolderDialog(QtWidgets.QDialog):
    """
    Browse Google Drive folders to choose the sync destination.

    Folders load lazily as the tree expands; a New Folder button creates a
    folder inside the current selection.  Selecting "My Drive" stores files
    at the Drive root.  A "Shared with me" section lists folders other
    accounts shared with this user (only folders the app itself created are
    visible to it); selecting the section itself is not a valid choice.
    """
    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.setWindowTitle(translate('CloudSyncPlugin.DriveFolderDialog', 'Choose Drive folder'))
        self.setMinimumSize(420, 380)
        layout = QtWidgets.QVBoxLayout(self)

        self.tree = QtWidgets.QTreeWidget(self)
        self.tree.setHeaderLabels([translate('CloudSyncPlugin.DriveFolderDialog', 'Folders')])
        self.tree.itemExpanded.connect(self._on_item_expanded)
        layout.addWidget(self.tree)

        self.error_label = QtWidgets.QLabel(self)
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons_row = QtWidgets.QHBoxLayout()
        self.new_folder_button = QtWidgets.QPushButton(self)
        self.new_folder_button.setText(translate('CloudSyncPlugin.DriveFolderDialog', 'New Folder...'))
        self.new_folder_button.clicked.connect(self._on_new_folder)
        buttons_row.addWidget(self.new_folder_button)
        buttons_row.addStretch()
        self.button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, self)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        buttons_row.addWidget(self.button_box)
        layout.addLayout(buttons_row)

        root = QtWidgets.QTreeWidgetItem(self.tree, ['My Drive'])
        root.setData(0, QtCore.Qt.UserRole, 'root')
        self._add_placeholder(root)
        shared = QtWidgets.QTreeWidgetItem(
            self.tree, [translate('CloudSyncPlugin.DriveFolderDialog', 'Shared with me')])
        shared.setData(0, QtCore.Qt.UserRole, 'shared')
        self._add_placeholder(shared)
        self.tree.expandItem(root)

    @staticmethod
    def _add_placeholder(item):
        placeholder = QtWidgets.QTreeWidgetItem(item, ['...'])
        placeholder.setData(0, QtCore.Qt.UserRole, None)

    @staticmethod
    def _item_folder_id(item):
        return item.data(0, QtCore.Qt.UserRole)

    def _remove_placeholders(self, item):
        for index in reversed(range(item.childCount())):
            if self._item_folder_id(item.child(index)) is None:
                item.removeChild(item.child(index))

    def _on_item_expanded(self, item):
        # Replace the placeholder with real children on first expand.
        if item.childCount() == 1 and self._item_folder_id(item.child(0)) is None:
            item.removeChild(item.child(0))
            self._load_children(item)

    def _load_children(self, item):
        folder_id = self._item_folder_id(item)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            if folder_id == 'root':
                folders = self.provider.list_folders(None)
            elif folder_id == 'shared':
                folders = self.provider.list_shared_folders()
            else:
                folders = self.provider.list_folders(folder_id)
        except Exception as error:
            self.error_label.setText(str(error))
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self.error_label.clear()
        for folder in folders:
            child = QtWidgets.QTreeWidgetItem(item, [folder.name])
            child.setData(0, QtCore.Qt.UserRole, folder.id)
            self._add_placeholder(child)

    def _on_new_folder(self):
        item = self.tree.currentItem() or self.tree.topLevelItem(0)
        # "Shared with me" is not a real folder; create under My Drive instead.
        if self._item_folder_id(item) == 'shared':
            item = self.tree.topLevelItem(0)
        folder_id = self._item_folder_id(item)
        parent_id = None if folder_id == 'root' else folder_id
        name, ok = QtWidgets.QInputDialog.getText(
            self,
            translate('CloudSyncPlugin.DriveFolderDialog', 'New Folder'),
            translate('CloudSyncPlugin.DriveFolderDialog', 'Folder name:'))
        if not ok or not name.strip():
            return
        try:
            new_folder = self.provider.create_folder(name.strip(), parent_id)
        except Exception as error:
            self.error_label.setText(str(error))
            return
        self.error_label.clear()
        self._remove_placeholders(item)
        child = QtWidgets.QTreeWidgetItem(item, [new_folder.name])
        child.setData(0, QtCore.Qt.UserRole, new_folder.id)
        self._add_placeholder(child)
        item.setExpanded(True)
        self.tree.setCurrentItem(child)

    def selected_folder(self):
        """
        :return: The chosen :class:`DriveFolder`, or None if nothing usable
            is selected.
        """
        from .providers import DriveFolder
        item = self.tree.currentItem()
        if item is None:
            return None
        folder_id = self._item_folder_id(item)
        if folder_id is None:
            return None
        if folder_id == 'root':
            return DriveFolder('root', 'My Drive')
        if folder_id == 'shared':
            # The "Shared with me" heading is not a folder.
            return None
        return DriveFolder(folder_id, item.text(0))


class CloudSyncTab(SettingsTab):
    """
    CloudSyncTab is the Cloud Sync settings tab in the settings dialog.
    """
    def setup_ui(self):
        """
        Set up the configuration tab UI.
        """
        self.setObjectName('CloudSyncTab')
        super(CloudSyncTab, self).setup_ui()
        # Sync behaviour group box
        self.behaviour_group_box = QtWidgets.QGroupBox(self.left_column)
        self.behaviour_group_box.setObjectName('behaviour_group_box')
        self.behaviour_layout = QtWidgets.QVBoxLayout(self.behaviour_group_box)
        self.behaviour_layout.setObjectName('behaviour_layout')
        self.enable_check_box = QtWidgets.QCheckBox(self.behaviour_group_box)
        self.enable_check_box.setObjectName('enable_check_box')
        self.behaviour_layout.addWidget(self.enable_check_box)
        self.sync_on_startup_check_box = QtWidgets.QCheckBox(self.behaviour_group_box)
        self.sync_on_startup_check_box.setObjectName('sync_on_startup_check_box')
        self.behaviour_layout.addWidget(self.sync_on_startup_check_box)
        self.sync_on_song_change_check_box = QtWidgets.QCheckBox(self.behaviour_group_box)
        self.sync_on_song_change_check_box.setObjectName('sync_on_song_change_check_box')
        self.behaviour_layout.addWidget(self.sync_on_song_change_check_box)
        # Debounce row
        self.debounce_widget = QtWidgets.QWidget(self.behaviour_group_box)
        self.debounce_layout = QtWidgets.QHBoxLayout(self.debounce_widget)
        self.debounce_layout.setContentsMargins(0, 0, 0, 0)
        self.debounce_label = QtWidgets.QLabel(self.debounce_widget)
        self.debounce_label.setObjectName('debounce_label')
        self.debounce_spin_box = QtWidgets.QSpinBox(self.debounce_widget)
        self.debounce_spin_box.setObjectName('debounce_spin_box')
        self.debounce_spin_box.setMinimum(10)
        self.debounce_spin_box.setMaximum(3600)
        self.debounce_layout.addWidget(self.debounce_label)
        self.debounce_layout.addWidget(self.debounce_spin_box)
        self.debounce_layout.addStretch()
        self.behaviour_layout.addWidget(self.debounce_widget)
        self.left_layout.addWidget(self.behaviour_group_box)

        # Google Drive group box
        self.drive_group_box = QtWidgets.QGroupBox(self.left_column)
        self.drive_group_box.setObjectName('drive_group_box')
        self.drive_layout = QtWidgets.QVBoxLayout(self.drive_group_box)
        self.drive_layout.setObjectName('drive_layout')
        # Provider row
        self.provider_widget = QtWidgets.QWidget(self.drive_group_box)
        self.provider_layout = QtWidgets.QHBoxLayout(self.provider_widget)
        self.provider_layout.setContentsMargins(0, 0, 0, 0)
        self.provider_label = QtWidgets.QLabel(self.provider_widget)
        self.provider_label.setObjectName('provider_label')
        self.provider_value_label = QtWidgets.QLabel(self.provider_widget)
        self.provider_value_label.setObjectName('provider_value_label')
        self.provider_layout.addWidget(self.provider_label)
        self.provider_layout.addWidget(self.provider_value_label)
        self.provider_layout.addStretch()
        self.drive_layout.addWidget(self.provider_widget)
        # Drive folder row
        self.folder_widget = QtWidgets.QWidget(self.drive_group_box)
        self.folder_layout = QtWidgets.QHBoxLayout(self.folder_widget)
        self.folder_layout.setContentsMargins(0, 0, 0, 0)
        self.folder_label = QtWidgets.QLabel(self.folder_widget)
        self.folder_label.setObjectName('folder_label')
        self.folder_line_edit = QtWidgets.QLineEdit(self.folder_widget)
        self.folder_line_edit.setObjectName('folder_line_edit')
        self.folder_line_edit.textEdited.connect(self._on_folder_name_edited)
        self.folder_button = QtWidgets.QPushButton(self.folder_widget)
        self.folder_button.setObjectName('folder_button')
        self.folder_button.clicked.connect(self.on_folder_browse)
        self.folder_layout.addWidget(self.folder_label)
        self.folder_layout.addWidget(self.folder_line_edit)
        self.folder_layout.addWidget(self.folder_button)
        self.folder_layout.addStretch()
        self.drive_layout.addWidget(self.folder_widget)
        # Drive folder chosen via the picker (id); empty until picked.
        self._folder_id = ''
        # Keep backups row
        self.keep_widget = QtWidgets.QWidget(self.drive_group_box)
        self.keep_layout = QtWidgets.QHBoxLayout(self.keep_widget)
        self.keep_layout.setContentsMargins(0, 0, 0, 0)
        self.keep_label = QtWidgets.QLabel(self.keep_widget)
        self.keep_label.setObjectName('keep_label')
        self.keep_spin_box = QtWidgets.QSpinBox(self.keep_widget)
        self.keep_spin_box.setObjectName('keep_spin_box')
        self.keep_spin_box.setMinimum(1)
        self.keep_spin_box.setMaximum(100)
        self.keep_layout.addWidget(self.keep_label)
        self.keep_layout.addWidget(self.keep_spin_box)
        self.keep_layout.addStretch()
        self.drive_layout.addWidget(self.keep_widget)
        # Buttons row
        self.buttons_widget = QtWidgets.QWidget(self.drive_group_box)
        self.buttons_layout = QtWidgets.QHBoxLayout(self.buttons_widget)
        self.buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.connect_button = QtWidgets.QPushButton(self.buttons_widget)
        self.connect_button.setObjectName('connect_button')
        self.connect_button.clicked.connect(self.on_connect)
        self.disconnect_button = QtWidgets.QPushButton(self.buttons_widget)
        self.disconnect_button.setObjectName('disconnect_button')
        self.disconnect_button.clicked.connect(self.on_disconnect)
        self.sync_now_button = QtWidgets.QPushButton(self.buttons_widget)
        self.sync_now_button.setObjectName('sync_now_button')
        self.sync_now_button.clicked.connect(self.on_sync_now)
        self.buttons_layout.addWidget(self.connect_button)
        self.buttons_layout.addWidget(self.disconnect_button)
        self.buttons_layout.addWidget(self.sync_now_button)
        self.buttons_layout.addStretch()
        self.drive_layout.addWidget(self.buttons_widget)
        self.left_layout.addWidget(self.drive_group_box)

        # Status label (right column)
        self.status_label = QtWidgets.QLabel(self.right_column)
        self.status_label.setObjectName('status_label')
        self.status_label.setWordWrap(True)
        self.right_layout.addWidget(self.status_label)
        self.right_layout.addStretch()

    def retranslate_ui(self):
        """
        Set the text of the UI widgets.
        """
        self.behaviour_group_box.setTitle(translate('CloudSyncPlugin.CloudSyncTab', 'Sync Behaviour'))
        self.enable_check_box.setText(translate('CloudSyncPlugin.CloudSyncTab', 'Enable cloud sync'))
        self.sync_on_startup_check_box.setText(
            translate('CloudSyncPlugin.CloudSyncTab', 'Sync when OpenLP starts'))
        self.sync_on_song_change_check_box.setText(
            translate('CloudSyncPlugin.CloudSyncTab', 'Sync after a song is created or updated'))
        self.debounce_label.setText(
            translate('CloudSyncPlugin.CloudSyncTab', 'Wait before syncing after a song change (seconds):'))
        self.drive_group_box.setTitle(translate('CloudSyncPlugin.CloudSyncTab', 'Google Drive'))
        self.provider_label.setText(translate('CloudSyncPlugin.CloudSyncTab', 'Provider:'))
        self.provider_value_label.setText(translate('CloudSyncPlugin.CloudSyncTab', 'Google Drive'))
        self.folder_label.setText(translate('CloudSyncPlugin.CloudSyncTab', 'Drive folder:'))
        self.folder_button.setText(translate('CloudSyncPlugin.CloudSyncTab', 'Browse...'))
        self.keep_label.setText(translate('CloudSyncPlugin.CloudSyncTab', 'Keep this many backups:'))
        self.connect_button.setText(translate('CloudSyncPlugin.CloudSyncTab', 'Connect'))
        self.disconnect_button.setText(translate('CloudSyncPlugin.CloudSyncTab', 'Disconnect'))
        self.sync_now_button.setText(translate('CloudSyncPlugin.CloudSyncTab', 'Sync Now'))
        self.refresh_status()

    def initialise(self):
        """
        Wire up controller signals.  The controller is only registered when
        the plugin is enabled, which may happen after this tab was created,
        so (re)try the connection every time the tab loads or an action is
        started -- otherwise the tab never hears the outcome and sits on
        "Connecting..." forever.
        """
        self._connect_controller_signals()

    def _connect_controller_signals(self):
        """
        Connect ``sync_finished`` once the controller exists.  Safe to call
        repeatedly; the connection is made at most once.
        """
        if getattr(self, '_signals_connected', False):
            return
        controller = self._controller()
        if controller is None:
            return
        controller.sync_finished.connect(self._on_sync_finished)
        self._signals_connected = True

    def _controller(self):
        """
        Return the plugin's SyncController, registered in the Registry by the plugin.
        """
        return Registry().get('cloudsync_controller')

    def _on_folder_name_edited(self):
        """
        A hand-typed folder name is resolved by name lookup, so forget any
        previously picked folder id.
        """
        self._folder_id = ''

    def on_folder_browse(self):
        """
        Open the Drive folder picker; remember the chosen folder by id so
        renames cannot break sync.
        """
        from . import auth
        from .providers import get_provider
        controller = self._controller()
        if controller is None:
            return
        credentials = auth.load_credentials(controller.token_path)
        if credentials is None:
            self.status_label.setText(
                translate('CloudSyncPlugin.CloudSyncTab', 'Connect to Google Drive first.'))
            return
        provider = get_provider(controller.provider_id)
        provider.connect(credentials)
        dialog = DriveFolderDialog(provider, parent=self)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            folder = dialog.selected_folder()
            if folder is not None:
                self.folder_line_edit.setText(folder.name)
                self._folder_id = folder.id
                self.changed = True

    def on_connect(self):
        """
        Save settings first, then run the OAuth flow in a worker thread.
        """
        self.save()
        self._connect_controller_signals()
        controller = self._controller()
        if controller is not None:
            self.status_label.setText(
                translate('CloudSyncPlugin.CloudSyncTab', 'Connecting to Google Drive...'))
            controller.authenticate()

    def on_disconnect(self):
        """
        Revoke and delete the cached OAuth token.
        """
        from . import auth
        controller = self._controller()
        if controller is not None:
            if auth.revoke_token(controller.token_path):
                self.status_label.setText(
                    translate('CloudSyncPlugin.CloudSyncTab', 'Disconnected.'))
            else:
                self.status_label.setText(
                    translate('CloudSyncPlugin.CloudSyncTab', 'No saved connection to remove.'))
            self.refresh_status()

    def on_sync_now(self):
        """
        Save settings first, then trigger a manual sync.
        """
        self.save()
        self._connect_controller_signals()
        controller = self._controller()
        if controller is not None:
            self.status_label.setText(
                translate('CloudSyncPlugin.CloudSyncTab', 'Syncing...'))
            controller.sync_now()

    def _on_sync_finished(self, success, message):
        """
        Show the outcome of a connect/sync operation started from this tab.
        """
        self.status_label.setText(message)
        self.refresh_status()

    def refresh_status(self):
        """
        Update the connect/disconnect button state based on whether a token is cached.
        """
        from . import auth
        controller = self._controller()
        connected = (controller is not None and auth.has_valid_token(controller.token_path))
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)

    def load(self):
        """
        Load the settings into the UI.
        """
        self.enable_check_box.setChecked(bool(self.settings.value('cloudsync/enabled')))
        self.sync_on_startup_check_box.setChecked(bool(self.settings.value('cloudsync/sync on startup')))
        self.sync_on_song_change_check_box.setChecked(bool(self.settings.value('cloudsync/sync on song change')))
        self.debounce_spin_box.setValue(int(self.settings.value('cloudsync/debounce seconds') or 60))
        self.folder_line_edit.setText(self.settings.value('cloudsync/drive folder') or 'OpenLP')
        self._folder_id = self.settings.value('cloudsync/drive folder id') or ''
        self.keep_spin_box.setValue(int(self.settings.value('cloudsync/keep backups') or 10))
        self.changed = False
        # The plugin may have been enabled since the tab was created.
        self._connect_controller_signals()

    def save(self):
        """
        Save the changes on exit of the Settings dialog.
        """
        self.settings.setValue('cloudsync/enabled', self.enable_check_box.isChecked())
        self.settings.setValue('cloudsync/sync on startup', self.sync_on_startup_check_box.isChecked())
        self.settings.setValue('cloudsync/sync on song change', self.sync_on_song_change_check_box.isChecked())
        self.settings.setValue('cloudsync/debounce seconds', self.debounce_spin_box.value())
        self.settings.setValue('cloudsync/drive folder', self.folder_line_edit.text())
        self.settings.setValue('cloudsync/drive folder id', self._folder_id)
        self.settings.setValue('cloudsync/keep backups', self.keep_spin_box.value())
        self.changed = False
