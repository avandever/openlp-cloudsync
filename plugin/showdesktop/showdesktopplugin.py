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
The :mod:`~openlp.plugins.showdesktop.showdesktopplugin` module wires the
Show Desktop on Startup plugin into OpenLP: it flips the live display
into "Show Desktop" mode once at application startup.
"""
import logging

from openlp.core.common.i18n import translate
from openlp.core.common.registry import Registry
from openlp.core.lib.plugin import Plugin, PluginStatus, StringContent
from openlp.core.state import State
from openlp.core.ui.icons import UiIcons

log = logging.getLogger(__name__)

# Default settings values registered under the ``showdesktop/`` key space.
# Note: ``showdesktop/status`` must be registered -- OpenLP 3.1.3's
# ``Settings.value()`` raises ``KeyError`` for any key missing from the
# defaults, and ``Plugin.set_status()`` reads it during startup.
showdesktop_settings = {
    'showdesktop/status': PluginStatus.Inactive,
}


class ShowdesktopPlugin(Plugin):
    """
    The Show Desktop on Startup plugin switches OpenLP's live display into
    "Show Desktop" mode once at application startup, so the projector
    output shows the desktop until the operator takes the display live.
    """
    def __init__(self):
        """
        Class __init__ method
        """
        super(ShowdesktopPlugin, self).__init__('showdesktop')
        self.weight = 0
        self.icon_path = UiIcons().desktop
        self.icon = self.icon_path
        self.settings.extend_default_settings(showdesktop_settings)
        State().add_service(self.name, self.weight, is_plugin=True)
        State().update_pre_conditions(self.name, self.check_pre_conditions())

    def initialise(self):
        """
        Initialise the plugin. Nothing to set up beyond the base class.
        """
        log.info('ShowDesktop Initialising')
        super(ShowdesktopPlugin, self).initialise()

    def finalise(self):
        """
        Tidy up on exit.
        """
        log.info('ShowDesktop Finalising')
        super(ShowdesktopPlugin, self).finalise()

    def app_startup(self):
        """
        Called once OpenLP has started up; switch the live display into
        "Show Desktop" mode.

        Timing: this runs from ``PluginManager.bootstrap_completion()``,
        which executes at the end of ``OpenLP.run()`` -- after the
        ``LiveController`` has been created (``loader()``), initialised
        (``bootstrap_initialise``, which builds its toolbar and sets up its
        displays) and after the main window has been shown. The live
        controller is therefore ready and no deferred ``QTimer.singleShot``
        call is needed. This method is also invoked when the plugin is
        toggled on mid-session from the Plugin Manager, where the main
        window and live controller exist as well.
        """
        log.info('ShowDesktop app_startup: enabling Show Desktop mode')
        try:
            live_controller = Registry().get('live_controller')
        except KeyError:
            log.warning('ShowDesktop: live_controller not available, skipping')
            return
        # This is the exact code path of clicking the "Show Desktop" toolbar
        # button: SlideController.on_toggle_desktop() calls
        # set_hide_mode(HideMode.Screen); toggle_display('desktop') is the
        # public dispatcher OpenLP itself uses (e.g. MainWindow calls
        # live_controller.toggle_display('desktop')).
        live_controller.toggle_display('desktop')

    @staticmethod
    def about():
        """
        Plugin Show Desktop on Startup about method.

        :return: text
        """
        about_text = translate('ShowdesktopPlugin', '<strong>Show Desktop on Startup Plugin</strong>'
                               '<br />This plugin automatically puts OpenLP into Show Desktop mode '
                               'when the application starts, so the projector output shows the desktop '
                               'until you take the display live.')
        return about_text

    def set_plugin_text_strings(self):
        """
        Called to define all translatable texts of the plugin.
        """
        # Name PluginList
        self.text_strings[StringContent.Name] = {
            'singular': translate('ShowdesktopPlugin', 'Show Desktop on Startup', 'name singular'),
            'plural': translate('ShowdesktopPlugin', 'Show Desktop on Startup', 'name plural')
        }
        # Name for MediaDockManager, SettingsManager
        self.text_strings[StringContent.VisibleName] = {
            'title': translate('ShowdesktopPlugin', 'Show Desktop on Startup', 'container title')
        }
