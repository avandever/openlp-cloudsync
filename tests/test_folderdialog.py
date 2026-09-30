"""
Tests for :class:`DriveFolderDialog`'s "Shared with me" support.

The dialog needs real Qt widgets, so these run on the session ``qapp``
fixture (a QApplication, offscreen).  ``openlp.core.lib.settingstab`` is
stubbed -- ``SettingsTab`` is only the base of ``CloudSyncTab``, which these
tests never instantiate -- and the ``cloudsynctab`` stub installed by
``test_plugin_bootstrap`` is removed first, so the real module is imported
no matter which test module pytest loads first.
"""
import sys
import types

import pytest

# test_plugin_bootstrap stubs this module in sys.modules at its own import
# time; drop that stub so the real dialog class is imported here.
sys.modules.pop('openlp.plugins.cloudsync.lib.cloudsynctab', None)


def _stub_module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


_stub_module('openlp.core.lib')
_stub_module('openlp.core.lib.settingstab', SettingsTab=type('SettingsTab', (), {}))

from openlp.plugins.cloudsync.lib.cloudsynctab import DriveFolderDialog  # noqa: E402
from openlp.plugins.cloudsync.lib.providers import DriveFolder  # noqa: E402
from openlp.plugins.cloudsync.lib.qtcompat import QtCore, QtWidgets  # noqa: E402


class FakeProvider:
    """Stand-in for GoogleDriveProvider's folder methods."""

    def __init__(self):
        self.calls = []
        self.shared = [DriveFolder('shared-1', 'Choir Songs')]
        self.children = {'shared-1': [DriveFolder('sub-1', 'Anthems')]}

    def list_folders(self, parent_id=None):
        self.calls.append(('list_folders', parent_id))
        if parent_id is None:
            return []
        return list(self.children.get(parent_id, []))

    def list_shared_folders(self):
        self.calls.append(('list_shared_folders',))
        return list(self.shared)

    def create_folder(self, name, parent_id=None):
        self.calls.append(('create_folder', name, parent_id))
        return DriveFolder('new-1', name)


@pytest.fixture
def dialog(qapp):
    provider = FakeProvider()
    dlg = DriveFolderDialog(provider)
    yield dlg
    dlg.close()


def _top_level_texts(dlg):
    return [dlg.tree.topLevelItem(i).text(0) for i in range(dlg.tree.topLevelItemCount())]


def test_dialog_shows_my_drive_and_shared_with_me(dialog):
    assert _top_level_texts(dialog) == ['My Drive', 'Shared with me']


def test_expanding_shared_section_lists_shared_folders(dialog):
    shared_item = dialog.tree.topLevelItem(1)
    dialog.tree.expandItem(shared_item)

    assert dialog.provider.calls[-1] == ('list_shared_folders',)
    assert shared_item.childCount() == 1
    child = shared_item.child(0)
    assert child.text(0) == 'Choir Songs'
    assert child.data(0, QtCore.Qt.UserRole) == 'shared-1'


def test_expanding_shared_child_loads_its_subfolders(dialog):
    shared_item = dialog.tree.topLevelItem(1)
    dialog.tree.expandItem(shared_item)
    child = shared_item.child(0)
    dialog.tree.expandItem(child)

    assert dialog.provider.calls[-1] == ('list_folders', 'shared-1')
    assert child.childCount() == 1
    assert child.child(0).text(0) == 'Anthems'


def test_selecting_shared_heading_is_not_a_folder(dialog):
    shared_item = dialog.tree.topLevelItem(1)
    dialog.tree.setCurrentItem(shared_item)

    assert dialog.selected_folder() is None


def test_selecting_shared_child_returns_its_id(dialog):
    shared_item = dialog.tree.topLevelItem(1)
    dialog.tree.expandItem(shared_item)
    child = shared_item.child(0)
    dialog.tree.setCurrentItem(child)

    folder = dialog.selected_folder()
    assert isinstance(folder, DriveFolder)
    assert (folder.id, folder.name) == ('shared-1', 'Choir Songs')


def test_new_folder_on_shared_heading_creates_under_my_drive(dialog, monkeypatch):
    monkeypatch.setattr(QtWidgets.QInputDialog, 'getText',
                        lambda *args, **kwargs: ('Youth', True))
    dialog.tree.setCurrentItem(dialog.tree.topLevelItem(1))

    dialog._on_new_folder()

    assert dialog.provider.calls[-1] == ('create_folder', 'Youth', None)
    my_drive = dialog.tree.topLevelItem(0)
    names = [my_drive.child(i).text(0) for i in range(my_drive.childCount())]
    assert 'Youth' in names


def test_new_folder_inside_shared_folder_uses_it_as_parent(dialog, monkeypatch):
    monkeypatch.setattr(QtWidgets.QInputDialog, 'getText',
                        lambda *args, **kwargs: ('Altos', True))
    shared_item = dialog.tree.topLevelItem(1)
    dialog.tree.expandItem(shared_item)
    dialog.tree.setCurrentItem(shared_item.child(0))

    dialog._on_new_folder()

    assert dialog.provider.calls[-1] == ('create_folder', 'Altos', 'shared-1')
