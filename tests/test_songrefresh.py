"""
Tests for the Cloud Sync plugin's live song-list refresh.

A fake plugin manager stands in for OpenLP's PluginManager so the refresh
logic can be exercised without the songs plugin.
"""
import pytest

from openlp.plugins.cloudsync.lib.songrefresh import refresh_songs_ui


class FakeSession:
    def __init__(self):
        self.expired = False

    def expire_all(self):
        self.expired = True


class FakeMediaItem:
    def __init__(self):
        self.searches = 0

    def on_search_text_button_clicked(self):
        self.searches += 1


class ExplodingMediaItem(FakeMediaItem):
    def on_search_text_button_clicked(self):
        raise RuntimeError('widget gone')


class FakeSongsPlugin:
    def __init__(self, media_item=True, session=True):
        self.media_item = FakeMediaItem() if media_item else None
        manager = type('FakeManager', (), {})()
        manager.session = FakeSession() if session else None
        self.manager = manager


class FakePluginManager:
    def __init__(self, plugin):
        self._plugin = plugin

    def get_plugin_by_name(self, name):
        assert name == 'songs'
        return self._plugin


def test_refresh_reruns_search_and_expires_session():
    plugin = FakeSongsPlugin()
    assert refresh_songs_ui(FakePluginManager(plugin)) is True
    assert plugin.media_item.searches == 1
    assert plugin.manager.session.expired is True


def test_missing_songs_plugin_returns_false():
    assert refresh_songs_ui(FakePluginManager(None)) is False


def test_missing_media_item_returns_false():
    plugin = FakeSongsPlugin(media_item=False)
    assert refresh_songs_ui(FakePluginManager(plugin)) is False


def test_missing_session_still_refreshes():
    plugin = FakeSongsPlugin(session=False)
    assert refresh_songs_ui(FakePluginManager(plugin)) is True
    assert plugin.media_item.searches == 1


def test_search_error_returns_false():
    plugin = FakeSongsPlugin()
    plugin.media_item = ExplodingMediaItem()
    assert refresh_songs_ui(FakePluginManager(plugin)) is False
