"""
Regression test for the OpenLP 3.1.3 startup crash.

OpenLP registers every ``Plugin`` subclass instance in the Registry under
``de_hump(ClassName)`` (see ``RegistryBase.__init__``), while
``State.update_pre_conditions(name, ...)`` looks the plugin back up as
``f'{name}_plugin'``.  When the class name does not de-hump to the plugin's
short name (``CloudSyncPlugin`` -> ``cloud_sync_plugin`` vs the ``cloudsync``
name passed to ``Plugin.__init__``), the lookup returns ``None`` and
``update_pre_conditions`` dereferences it::

    AttributeError: 'NoneType' object has no attribute 'name'

``PluginManager.bootstrap_initialise`` only catches ``TypeError``, so the
``AttributeError`` escapes and OpenLP dies on startup.  This test replays
that exact 3.1.3 flow against the real plugin module with faithful
re-implementations of the 3.1.3 pieces involved, so the convention
``de_hump(ClassName) == f'{plugin.name}_plugin'`` is enforced going forward.

The test is self-contained: it stubs the ``openlp.core`` modules the plugin
imports (they pull in Qt / the full app stack) and the two Qt-dependent
collaborators, and needs no display or Qt bindings.
"""
import re
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Verbatim pieces of OpenLP 3.1.3 involved in the bug
# ---------------------------------------------------------------------------

# From openlp/core/common/__init__.py (identical in 3.1.3 and master)
FIRST_CAMEL_REGEX = re.compile('(.)([A-Z][a-z]+)')
SECOND_CAMEL_REGEX = re.compile('([a-z0-9])([A-Z])')


def de_hump(name):
    """Change any Camel Case string to python string (verbatim 3.1.3)."""
    sub_name = FIRST_CAMEL_REGEX.sub(r'\1_\2', name)
    return SECOND_CAMEL_REGEX.sub(r'\1_\2', sub_name).lower()


class FakeRegistry:
    """Minimal stand-in for openlp.core.common.registry.Registry."""

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


class FakeSettings:
    """Minimal stand-in for OpenLP's Settings object.

    Faithful to openlp 3.1.3 in the one way that matters here:
    ``value()`` raises ``KeyError`` for keys that were never registered via
    ``extend_default_settings`` (see ``Settings.value`` in
    openlp/core/common/settings.py).
    """

    _defaults = {}

    def __init__(self):
        self._values = {}

    def value(self, key, default=None):
        if key not in FakeSettings._defaults:
            raise KeyError(key)
        return self._values.get(key, FakeSettings._defaults[key])

    def setValue(self, key, value):
        self._values[key] = value

    def extend_default_settings(self, defaults):
        for key, value in defaults.items():
            FakeSettings._defaults.setdefault(key, value)


class PluginStatus:
    """Minimal stand-in for openlp.core.lib.plugin.PluginStatus."""

    Active = 'active'
    Inactive = 'inactive'
    Disabled = 'disabled'


class Plugin:
    """
    Mirrors the ordering of openlp 3.1.3 ``Plugin.__init__``: the very first
    thing it does is ``super().__init__()``, i.e. ``RegistryBase.__init__``,
    which registers the instance under ``de_hump(class name)`` -- long before
    the subclass body runs ``State().update_pre_conditions(...)``.
    """

    def __init__(self, name, media_item_class=None, settings_tab_class=None, version=None):
        # RegistryBase.__init__ (openlp/core/common/registry.py, verbatim 3.1.3):
        #   Registry().register(de_hump(self.__class__.__name__), self)
        FakeRegistry().register(de_hump(self.__class__.__name__), self)
        self.name = name
        self.settings = FakeSettings()
        self.status = 'inactive'

    def check_pre_conditions(self):
        # Base implementation (openlp 3.1.3 openlp/core/lib/plugin.py); the
        # cloudsync plugin does not override it, so pre-conditions pass and
        # update_pre_conditions takes the branch that crashed.
        return True

    def set_status(self):
        # Verbatim 3.1.3: self.status = self.settings.value(f'{self.name}/status')
        self.status = self.settings.value('{name}/status'.format(name=self.name))


class _StateModule:
    pass


class State:
    """Minimal State with update_pre_conditions verbatim from openlp 3.1.3."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            instance = super().__new__(cls)
            instance.modules = {}
            cls._instance = instance
        return cls._instance

    def add_service(self, name, order, is_plugin=False, status=None, requires=None):
        if name not in self.modules:
            module = _StateModule()
            module.name = name
            module.order = order
            module.is_plugin = is_plugin
            module.status = status
            module.pass_preconditions = None
            self.modules[name] = module

    def log_debug(self, message):
        pass

    def update_pre_conditions(self, name: str, is_active: bool):
        # Verbatim from openlp 3.1.3 openlp/core/state.py -- this is the frame
        # from Andrew's traceback (state.py line 129).
        self.modules[name].pass_preconditions = is_active
        if self.modules[name].is_plugin:
            plugin = FakeRegistry().get('{name}_plugin'.format(name=name))
            if is_active:
                self.log_debug('Plugin {name} active'.format(name=plugin.name))
                plugin.set_status()
            else:
                plugin.status = 'disabled'


# ---------------------------------------------------------------------------
# Stub the openlp.core modules the plugin module imports
# ---------------------------------------------------------------------------

def _stub_module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _ActionList:
    @classmethod
    def get_instance(cls):
        return cls()


class _UiStrings:
    Tools = 'Tools'


def _translate(context, text, *args, **kwargs):
    return text


class _StringContent:
    Name = 'name'


def _create_action(*args, **kwargs):
    return types.SimpleNamespace()


class _UiIcons:
    alert = 'alert'


_stub_module('openlp.core.common')
_stub_module('openlp.core.common.actions', ActionList=_ActionList)
_stub_module('openlp.core.common.i18n', UiStrings=_UiStrings, translate=_translate)
_stub_module('openlp.core.common.registry', Registry=FakeRegistry)
_stub_module('openlp.core.lib')
_stub_module('openlp.core.lib.plugin', Plugin=Plugin, PluginStatus=PluginStatus,
              StringContent=_StringContent)
_stub_module('openlp.core.lib.ui', create_action=_create_action)
_stub_module('openlp.core.state', State=State)
_stub_module('openlp.core.ui')
_stub_module('openlp.core.ui.icons', UiIcons=_UiIcons)
# Qt-dependent collaborators of the plugin module; the bug lives entirely in
# cloudsyncplugin.py's __init__ / class name, so dummies are appropriate here.
# (The lib package itself is stubbed so its real __init__ -> .backup chain is
# never executed; the two submodules below are pre-seeded in sys.modules.)
import os as _os
_TEST_DIR = _os.path.dirname(_os.path.abspath(__file__))
_REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.dirname(_TEST_DIR)))
_plugin_dir = _os.path.join(_REPO_ROOT, 'openlp', 'plugins', 'cloudsync')
_lib_stub = _stub_module('openlp.plugins.cloudsync.lib')
_lib_stub.__path__ = [_os.path.join(_plugin_dir, 'lib')]
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_stub_module('openlp.plugins.cloudsync.lib.cloudsynctab', CloudSyncTab=type('CloudSyncTab', (), {}))
_stub_module('openlp.plugins.cloudsync.lib.synccontroller', SyncController=type('SyncController', (), {}))

import openlp.plugins.cloudsync.cloudsyncplugin as plugin_module  # noqa: E402


def _plugin_class():
    """Find the Plugin subclass defined in cloudsyncplugin.py without
    hard-coding its name, so a future rename fails with a clear message."""
    subclasses = [cls for cls in Plugin.__subclasses__()
                  if cls.__module__ == plugin_module.__name__]
    assert len(subclasses) == 1, \
        'expected exactly one Plugin subclass in cloudsyncplugin, found {count}'.format(count=len(subclasses))
    return subclasses[0]


@pytest.fixture(autouse=True)
def _fresh_singletons():
    FakeRegistry._instance = None
    State._instance = None
    FakeSettings._defaults = {}
    yield
    FakeRegistry._instance = None
    State._instance = None
    FakeSettings._defaults = {}


def test_plugin_class_name_de_humps_to_registry_key():
    """
    The OpenLP convention every built-in plugin follows: the class name must
    de-hump to '<plugin name>_plugin', because that is the Registry key
    State.update_pre_conditions() looks up.
    """
    plugin_class = _plugin_class()
    assert de_hump(plugin_class.__name__) == 'cloudsync_plugin'


def test_bootstrap_instantiation_does_not_raise():
    """
    Replay what PluginManager.bootstrap_initialise does on 3.1.3: instantiate
    the plugin class.  Before the fix this raised
    ``AttributeError: 'NoneType' object has no attribute 'name'`` from
    ``State.update_pre_conditions`` -- an exception bootstrap_initialise does
    not catch, which killed OpenLP on startup.
    """
    plugin_class = _plugin_class()
    plugin = plugin_class()  # must not raise
    assert plugin.name == 'cloudsync'
    assert FakeRegistry().get('cloudsync_plugin') is plugin
    assert State().modules['cloudsync'].pass_preconditions is True
    # set_status() read the registered default without raising KeyError
    assert plugin.status == PluginStatus.Inactive


def test_shortcut_actions_have_registered_defaults():
    """
    openlp 3.1.3's ``ActionList.add_action()`` calls
    ``Settings.get_default_value('shortcuts/<action name>')`` for every action
    created with ``can_shortcuts=True`` -- and that raises ``KeyError`` for any
    key missing from the registered defaults (verbatim 3.1.3 behaviour).

    This statically checks the plugin source so the crash is caught here
    instead of on Andrew's machine.
    """
    import ast

    source = open(_os.path.join(_plugin_dir, 'cloudsyncplugin.py'), encoding='utf-8').read()
    tree = ast.parse(source)
    missing = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'create_action'):
            continue
        if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
            continue
        name = node.args[1].value
        wants_shortcuts = any(
            kw.arg == 'can_shortcuts' and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        if wants_shortcuts and 'shortcuts/{}'.format(name) not in plugin_module.cloudsync_settings:
            missing.append(name)
    assert not missing, (
        'create_action(..., can_shortcuts=True) without a registered default: %s' % missing
    )
