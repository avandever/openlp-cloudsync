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
The :mod:`~openlp.plugins.cloudsync.cloudsyncplugin` module wires the
Cloud Sync plugin into OpenLP: it registers the settings tab, the sync
controller, the ``song_changed`` hook, the startup sync, and the
**Tools** menu item.
"""
import logging

from openlp.core.common.actions import ActionList
from openlp.core.common.i18n import UiStrings, translate
from openlp.core.common.registry import Registry
from openlp.core.lib.plugin import Plugin, PluginStatus, StringContent
from openlp.core.lib.ui import create_action
from openlp.core.state import State
from openlp.core.ui.icons import UiIcons
from .lib.cloudsynctab import CloudSyncTab
from .lib.synccontroller import SyncController

log = logging.getLogger(__name__)

# Default settings values registered under the ``cloudsync/`` key space.
# Note: ``cloudsync/status`` must be registered -- OpenLP 3.1.3's
# ``Settings.value()`` raises ``KeyError`` for any key missing from the
# defaults, and ``Plugin.set_status()`` reads it during startup.
# Note: ``shortcuts/toolsCloudSyncItem`` must be registered -- 3.1.3's
# ``ActionList.add_action()`` calls ``Settings.get_default_value()`` for every
# action created with ``can_shortcuts=True``, which also raises ``KeyError``
# for unregistered keys (built-ins register all of theirs the same way).
cloudsync_settings = {
    'cloudsync/status': PluginStatus.Inactive,
    'shortcuts/toolsCloudSyncItem': [],
    'cloudsync/enabled': False,
    'cloudsync/provider': 'googledrive',
    'cloudsync/drive folder': 'OpenLP',
    'cloudsync/drive folder id': '',
    'cloudsync/sync on startup': True,
    'cloudsync/sync on song change': True,
    'cloudsync/debounce seconds': 60,
    'cloudsync/keep backups': 10,
}


class CloudsyncPlugin(Plugin):
    """
    The Cloud Sync plugin keeps the OpenLP library synchronised with a
    cloud storage provider (Google Drive) in the background.
    """
    def __init__(self):
        """
        Class __init__ method
        """
        super(CloudsyncPlugin, self).__init__('cloudsync', settings_tab_class=CloudSyncTab)
        self.weight = -4
        self.icon_path = UiIcons().alert
        self.icon = self.icon_path
        self.settings.extend_default_settings(cloudsync_settings)
        self.controller = None
        State().add_service(self.name, self.weight, is_plugin=True)
        State().update_pre_conditions(self.name, self.check_pre_conditions())

    def initialise(self):
        """
        Initialise the plugin: create the sync controller, register it so the
        settings tab can reach it, and hook song changes.
        """
        log.info('CloudSync Initialising')
        super(CloudsyncPlugin, self).initialise()
        self.controller = SyncController(self.settings)
        Registry().register('cloudsync_controller', self.controller)
        Registry().register_function('song_changed', self.controller.on_song_changed)
        self.tools_sync_item.setVisible(True)
        action_list = ActionList.get_instance()
        action_list.add_action(self.tools_sync_item, UiStrings().Tools)

    def finalise(self):
        """
        Tidy up on exit.
        """
        log.info('CloudSync Finalising')
        super(CloudsyncPlugin, self).finalise()
        if getattr(self, 'tools_sync_item', None):
            self.tools_sync_item.setVisible(False)
            action_list = ActionList.get_instance()
            action_list.remove_action(self.tools_sync_item, UiStrings().Tools)

    def app_startup(self):
        """
        Called once OpenLP has started up; kicks off the background sync.
        """
        log.info('CloudSync app_startup')
        if self.controller is not None:
            self.controller.startup_sync()

    def add_tools_menu_item(self, tools_menu):
        """
        Give the Cloud Sync plugin the opportunity to add items to the **Tools** menu.

        :param tools_menu: The actual **Tools** menu item, so that your actions can use it as their parent.
        """
        log.info('add tools menu')
        self.tools_sync_item = create_action(
            tools_menu, 'toolsCloudSyncItem',
            text=translate('CloudSyncPlugin', '&Sync Library Now'),
            icon=UiIcons().alert,
            statustip=translate('CloudSyncPlugin', 'Sync the library with cloud storage now.'),
            visible=False, can_shortcuts=True, triggers=self.on_sync_now_trigger)
        self.main_window.tools_menu.addAction(self.tools_sync_item)

    def on_sync_now_trigger(self):
        """
        Handler for the **Tools** menu item: run a manual sync.
        """
        if self.controller is not None:
            self.controller.sync_now()

    @staticmethod
    def about():
        """
        Plugin Cloud Sync about method.

        :return: text
        """
        about_text = translate('CloudSyncPlugin', '<strong>Cloud Sync Plugin</strong>'
                               '<br />The cloud sync plugin automatically synchronises '
                               'your OpenLP library with cloud storage in the background.')
        return about_text

    def set_plugin_text_strings(self):
        """
        Called to define all translatable texts of the plugin.
        """
        # Name PluginList
        self.text_strings[StringContent.Name] = {
            'singular': translate('CloudSyncPlugin', 'Cloud Sync', 'name singular'),
            'plural': translate('CloudSyncPlugin', 'Cloud Sync', 'name plural')
        }
        # Name for MediaDockManager, SettingsManager
        self.text_strings[StringContent.VisibleName] = {
            'title': translate('CloudSyncPlugin', 'Cloud Sync', 'container title')
        }
