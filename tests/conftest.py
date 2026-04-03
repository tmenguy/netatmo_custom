"""Root conftest for netatmo custom component tests.

Solves three problems so HA core test files work unmodified:

1. pyatmo: Tests do `from pyatmo.const import ALL_SCOPES` and
   `patch("pyatmo.home.Home.async_set_state")`. Our pyatmo is bundled at
   custom_components/netatmo/pyatmo/. We register it under both
   `pyatmo.*` and `custom_components.netatmo.pyatmo.*` so there is a
   single set of module objects — patches land on the real classes.

2. homeassistant.components.netatmo: Tests do
   `from homeassistant.components.netatmo.const import DOMAIN` and
   `patch("homeassistant.components.netatmo.data_handler.PLATFORMS")`.
   We pre-register a lightweight stub whose __path__ points to our
   custom_components/netatmo/, replaced by the real modules in Phase 4.

3. homeassistant.components.cloud: The real cloud component cascades into
   dozens of optional HA deps. We pre-register a stub with the symbols
   the netatmo integration needs at runtime.
"""

import importlib
import importlib.util
from pathlib import Path
import sys
import types

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CC_NETATMO = _PROJECT_ROOT / "custom_components" / "netatmo"
_PYATMO_DIR = _CC_NETATMO / "pyatmo"

# Ensure custom_components is importable
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Phase 1 (module level): Register bundled pyatmo under both top-level
# `pyatmo.*` and `custom_components.netatmo.pyatmo.*` names so there is
# ONE set of module objects. This means:
#   - `import pyatmo` (in integration code) → same object
#   - `from . import pyatmo` (in __init__.py) → same object
#   - `patch("pyatmo.home.Home.X")` (in tests) → patches the real class
# ---------------------------------------------------------------------------

# Ensure the custom_components.netatmo namespace package stubs exist
# so Python accepts custom_components.netatmo.pyatmo.* module names
if "custom_components" not in sys.modules:
    _cc = types.ModuleType("custom_components")
    _cc.__path__ = [str(_CC_NETATMO.parent)]
    _cc.__package__ = "custom_components"
    sys.modules["custom_components"] = _cc

if "custom_components.netatmo" not in sys.modules:
    _cc_netatmo_stub = types.ModuleType("custom_components.netatmo")
    _cc_netatmo_stub.__path__ = [str(_CC_NETATMO)]
    _cc_netatmo_stub.__package__ = "custom_components.netatmo"
    sys.modules["custom_components.netatmo"] = _cc_netatmo_stub

# Load pyatmo via importlib (avoids sys.path pollution from select.py etc.)
_pyatmo_spec = importlib.util.spec_from_file_location(
    "pyatmo",
    str(_PYATMO_DIR / "__init__.py"),
    submodule_search_locations=[str(_PYATMO_DIR)],
)
_pyatmo_mod = importlib.util.module_from_spec(_pyatmo_spec)

# Register under BOTH names — single identity
sys.modules["pyatmo"] = _pyatmo_mod
sys.modules["custom_components.netatmo.pyatmo"] = _pyatmo_mod
sys.modules["custom_components.netatmo"].pyatmo = _pyatmo_mod

_pyatmo_spec.loader.exec_module(_pyatmo_mod)

# Register pyatmo subpackages under both names
for _sub in _PYATMO_DIR.iterdir():
    if _sub.is_dir() and (_sub / "__init__.py").exists():
        _top_name = f"pyatmo.{_sub.name}"
        _cc_name = f"custom_components.netatmo.pyatmo.{_sub.name}"
        _sub_spec = importlib.util.spec_from_file_location(
            _top_name,
            str(_sub / "__init__.py"),
            submodule_search_locations=[str(_sub)],
        )
        _sub_mod = importlib.util.module_from_spec(_sub_spec)
        # Single identity: both names point to same object
        sys.modules[_top_name] = _sub_mod
        sys.modules[_cc_name] = _sub_mod
        setattr(_pyatmo_mod, _sub.name, _sub_mod)
        _sub_spec.loader.exec_module(_sub_mod)

# Also register individual .py files as submodules (pyatmo.home, pyatmo.const, etc.)
for _file in _PYATMO_DIR.glob("*.py"):
    if _file.name == "__init__.py":
        continue
    _mod_base = _file.stem
    _top_name = f"pyatmo.{_mod_base}"
    _cc_name = f"custom_components.netatmo.pyatmo.{_mod_base}"
    if _top_name not in sys.modules:
        _file_spec = importlib.util.spec_from_file_location(
            _top_name, str(_file)
        )
        _file_mod = importlib.util.module_from_spec(_file_spec)
        sys.modules[_top_name] = _file_mod
        sys.modules[_cc_name] = _file_mod
        setattr(_pyatmo_mod, _mod_base, _file_mod)
        _file_spec.loader.exec_module(_file_mod)

# Register nested submodules (pyatmo.modules.netatmo, pyatmo.modules.base_class, etc.)
for _sub in _PYATMO_DIR.iterdir():
    if _sub.is_dir() and (_sub / "__init__.py").exists():
        for _nested in _sub.glob("*.py"):
            if _nested.name == "__init__.py":
                continue
            _mod_base = _nested.stem
            _top_name = f"pyatmo.{_sub.name}.{_mod_base}"
            _cc_name = f"custom_components.netatmo.pyatmo.{_sub.name}.{_mod_base}"
            if _top_name not in sys.modules:
                _nested_spec = importlib.util.spec_from_file_location(
                    _top_name, str(_nested)
                )
                _nested_mod = importlib.util.module_from_spec(_nested_spec)
                sys.modules[_top_name] = _nested_mod
                sys.modules[_cc_name] = _nested_mod
                # parent subpackage
                _parent = sys.modules.get(f"pyatmo.{_sub.name}")
                if _parent:
                    setattr(_parent, _mod_base, _nested_mod)
                _nested_spec.loader.exec_module(_nested_mod)


# ---------------------------------------------------------------------------
# Phase 2 (module level): Pre-register homeassistant.components.netatmo as a
# lightweight stub package pointing to our custom component directory.
# ---------------------------------------------------------------------------

_netatmo_stub = types.ModuleType("homeassistant.components.netatmo")
_netatmo_stub.__path__ = [str(_CC_NETATMO)]
_netatmo_stub.__file__ = str(_CC_NETATMO / "__init__.py")
_netatmo_stub.__package__ = "homeassistant.components.netatmo"

# Constants that test files import directly from the package
_netatmo_stub.DOMAIN = "netatmo"
_netatmo_stub.DATA_CAMERAS = "cameras"
_netatmo_stub.DATA_EVENTS = "netatmo_events"

sys.modules["homeassistant.components.netatmo"] = _netatmo_stub

# ---------------------------------------------------------------------------
# Phase 3 (module level): Pre-register homeassistant.components.cloud stub.
# ---------------------------------------------------------------------------

if "homeassistant.components.cloud" not in sys.modules:
    import enum

    import homeassistant.components

    _cloud_dir = str(Path(homeassistant.components.__path__[0]) / "cloud")

    _cloud_stub = types.ModuleType("homeassistant.components.cloud")
    _cloud_stub.__path__ = [_cloud_dir]
    _cloud_stub.__file__ = str(Path(_cloud_dir) / "__init__.py")
    _cloud_stub.__package__ = "homeassistant.components.cloud"
    _cloud_stub.DOMAIN = "cloud"

    # Functions used by the netatmo integration at runtime
    _cloud_stub.async_active_subscription = lambda hass: False
    _cloud_stub.async_is_connected = lambda hass: False
    _cloud_stub.async_listen_connection_change = lambda hass, cb: lambda: None
    _cloud_stub.async_create_cloudhook = None
    _cloud_stub.async_delete_cloudhook = None

    class _CloudConnectionState(enum.Enum):
        CLOUD_CONNECTED = "cloud_connected"
        CLOUD_DISCONNECTED = "cloud_disconnected"

    _cloud_stub.CloudConnectionState = _CloudConnectionState

    class _CloudNotAvailable(Exception):
        pass

    _cloud_stub.CloudNotAvailable = _CloudNotAvailable

    # Provide async_setup so async_setup_component("cloud", ...) can succeed
    async def _cloud_async_setup(hass, config):
        return True

    _cloud_stub.async_setup = _cloud_async_setup

    sys.modules["homeassistant.components.cloud"] = _cloud_stub

# ---------------------------------------------------------------------------
# Phase 4 (fixtures): Finalize aliasing after HA environment is ready
# ---------------------------------------------------------------------------

from collections.abc import Generator  # noqa: E402
from unittest.mock import patch as mock_patch  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all tests."""
    return


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Test fixture that ensures all entities are enabled in the registry."""
    with (
        mock_patch(
            "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
            return_value=True,
        ),
        mock_patch(
            "homeassistant.components.device_tracker.config_entry.ScannerEntity.entity_registry_enabled_default",
            return_value=True,
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _alias_netatmo_modules(hass):
    """Finalize module aliasing after HA is initialized.

    Replaces the Phase 1 stubs with the actual custom component modules
    so that mock.patch targets the right objects.
    """
    # Ensure our custom_components dir is in the namespace package path
    # (phcc registers its own testing_config/custom_components which shadows ours)
    import custom_components

    our_cc = str(_CC_NETATMO.parent)
    if our_cc not in custom_components.__path__:
        custom_components.__path__.insert(0, our_cc)

    # Phase 1 registered a lightweight stub for custom_components.netatmo.
    # Replace it with the real module by force-loading __init__.py.
    # HA is fully ready (hass fixture), so the real __init__.py can run safely.
    _spec = importlib.util.spec_from_file_location(
        "custom_components.netatmo",
        str(_CC_NETATMO / "__init__.py"),
        submodule_search_locations=[str(_CC_NETATMO)],
    )
    cc_netatmo = importlib.util.module_from_spec(_spec)
    # Preserve the pyatmo identity from Phase 1
    cc_netatmo.pyatmo = sys.modules["pyatmo"]
    sys.modules["custom_components.netatmo"] = cc_netatmo
    _spec.loader.exec_module(cc_netatmo)

    # Also alias as homeassistant.components.netatmo
    sys.modules["homeassistant.components.netatmo"] = cc_netatmo

    # Walk non-pyatmo submodules and alias them under both prefixes
    import pkgutil

    for _importer, modname, _ispkg in pkgutil.walk_packages(
        cc_netatmo.__path__,
        prefix="custom_components.netatmo.",
    ):
        if ".pyatmo" in modname:
            continue
        try:
            mod = importlib.import_module(modname)
            alias = modname.replace(
                "custom_components.netatmo", "homeassistant.components.netatmo"
            )
            sys.modules[alias] = mod
        except ImportError:
            pass
