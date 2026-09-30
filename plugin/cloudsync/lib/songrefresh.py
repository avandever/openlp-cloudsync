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
The :mod:`~..songrefresh` module refreshes the songs list in the running
OpenLP instance after a cloud merge.

A merge writes straight to the live ``songs.sqlite`` file, but the songs
plugin keeps its list in memory, which is why earlier versions asked for a
restart.  The songs plugin rebuilds that list from the database every time a
search runs -- the same refresh every song import, edit or delete triggers --
so after a merge we simply re-run the current search instead of restarting.
"""
import logging

log = logging.getLogger(__name__)


def refresh_songs_ui(plugin_manager=None):
    """
    Ask the songs plugin to rebuild its visible song list from the database.

    Must be called on the GUI thread: it drives the songs media item, the
    same refresh every song import, edit or delete triggers, so merged songs
    appear without restarting OpenLP.

    :param plugin_manager: Optional plugin manager (or class) exposing
        ``get_plugin_by_name``.  Imported from OpenLP when omitted, which
        also makes the function easy to test with a fake.
    :return: True when the list was refreshed; False when the songs plugin
        is unavailable, in which case the caller should fall back to asking
        the user to restart OpenLP.
    """
    try:
        if plugin_manager is None:
            from openlp.core.lib.pluginmanager import PluginManager
            plugin_manager = PluginManager
        songs_plugin = plugin_manager.get_plugin_by_name('songs')
        media_item = getattr(songs_plugin, 'media_item', None)
        if media_item is None:
            log.debug('Songs plugin has no media item; cannot refresh the song list')
            return False
        # Drop any cached ORM state so the re-query sees the merged rows
        # instead of stale in-memory copies.
        session = getattr(getattr(songs_plugin, 'manager', None), 'session', None)
        expire_all = getattr(session, 'expire_all', None)
        if callable(expire_all):
            try:
                expire_all()
            except Exception:
                log.warning('Could not expire the songs database session', exc_info=True)
        # Re-run the current search: the canonical "reload the list" used by
        # import, edit, delete, clone and reindex.
        media_item.on_search_text_button_clicked()
        log.info('Song list refreshed after cloud merge')
        return True
    except Exception:
        log.exception('Could not refresh the song list after cloud merge')
        return False
