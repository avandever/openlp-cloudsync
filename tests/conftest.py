"""Local fixtures for the cloudsync plugin tests.

These keep the suite runnable without loading the full OpenLP application
stack (the top-level tests/conftest.py imports openlp.core.app, which pulls
in the entire dependency tree).

The ``openlp.core`` modules the plugin's library code imports are stubbed in
``sys.modules`` here, before any test module imports them.  Run this directory
with ``--confcutdir`` so the top-level conftest (which needs the real app and
Qt bindings) is not loaded::

    python3 -m pytest --confcutdir=tests/openlp_plugins/cloudsync \\
        tests/openlp_plugins/cloudsync/
"""
import sys
import types
from pathlib import Path

import pytest


def _stub_module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _AppLocation:
    """Stand-in returning temp directories for the paths the plugin uses."""

    _base = None

    @classmethod
    def _get_base(cls):
        if cls._base is None:
            import tempfile
            cls._base = Path(tempfile.mkdtemp(prefix='cloudsync-test-'))
        return cls._base

    @classmethod
    def get_data_path(cls):
        return cls._get_base()

    @classmethod
    def get_section_data_path(cls, section):
        path = cls._get_base() / section
        path.mkdir(parents=True, exist_ok=True)
        return path


def _translate(context, text, *args, **kwargs):
    return text


class _Registry:
    """Minimal singleton stand-in for openlp.core.common.registry.Registry."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            instance = super().__new__(cls)
            instance.service_list = {}
            cls._instance = instance
        return cls._instance

    def register(self, key, reference):
        self.service_list[key] = reference

    def get(self, key):
        return self.service_list.get(key)

    def register_function(self, event, function):
        pass


def _is_thread_finished(thread_name):
    return True


def _run_thread(worker, thread_name):
    # The controller only inspects this signature for 'queued_connections';
    # tests never actually run background threads.
    raise AssertionError('run_thread should not be called in unit tests')


_stub_module('openlp.core.common')
_stub_module('openlp.core.common.applocation', AppLocation=_AppLocation)
_stub_module('openlp.core.common.i18n', translate=_translate)
_stub_module('openlp.core.common.registry', Registry=_Registry)
_stub_module('openlp.core.threading',
             ThreadWorker=type('ThreadWorker', (), {}),
             is_thread_finished=_is_thread_finished,
             run_thread=_run_thread)


class FakeSettings:
    """Minimal dict-backed stand-in for OpenLP's Settings object."""

    def __init__(self):
        self._values = {}

    def value(self, key, default=None):
        return self._values.get(key, default)

    def setValue(self, key, value):
        self._values[key] = value


@pytest.fixture
def settings():
    return FakeSettings()


@pytest.fixture(scope='session')
def qapp():
    """A Qt application instance for tests that instantiate QObjects.

    The top-level conftest's ``qapp`` builds the whole OpenLP application
    (needs PySide6 and the full stack); the cloudsync suite only needs an
    application object to exist, so a bare application from the Qt binding
    the plugin actually imported (via ``qtcompat``) is enough.  It is a
    full QApplication (offscreen) rather than a QCoreApplication so widget
    tests -- e.g. the folder picker dialog -- can run in this suite too.
    """
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from openlp.plugins.cloudsync.lib.qtcompat import QtWidgets

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    yield app
