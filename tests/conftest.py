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
# exec_module naturally loads all submodules (modules/__init__.py imports
# .netatmo, .legrand, etc.), so we get a complete, consistent module tree.
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

# After exec_module, all pyatmo submodules are naturally loaded in sys.modules.
# Alias each one under custom_components.netatmo.pyatmo.* so both paths
# resolve to the SAME module object (critical for unittest.mock.patch).
_CC_PYATMO_PREFIX = "custom_components.netatmo.pyatmo."
for _key in list(sys.modules):
    if _key.startswith("pyatmo."):
        _cc_key = _CC_PYATMO_PREFIX + _key[len("pyatmo."):]
        sys.modules[_cc_key] = sys.modules[_key]


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
# Phase 3 (fixtures): Finalize aliasing after HA environment is ready
# ---------------------------------------------------------------------------

from collections.abc import Generator  # noqa: E402
from unittest.mock import patch as mock_patch  # noqa: E402

import pytest  # noqa: E402

import homeassistant.util.dt as _ha_dt_util  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all tests."""
    return


@pytest.fixture(autouse=True)
def _patch_data_handler_time():
    """Patch time() and asyncio.sleep() in data_handler for frozen time.

    The data handler uses ``from time import time`` for rate limiting and
    ``await asyncio.sleep(delta_sleep)`` to space out API calls. In tests
    with frozen time, wall-clock ``time()`` never advances (causing rate
    limit deadlocks) and real ``asyncio.sleep()`` blocks for seconds per
    candidate (causing timeouts).

    Patching ``time`` to use HA's frozen clock and ``asyncio.sleep`` to
    a no-op keeps the rate limiter consistent while avoiding real waits.
    """
    _orig_sleep = importlib.import_module("asyncio").sleep

    async def _fast_sleep(delay):
        """Skip intentional inter-call delays, preserve zero-length yields."""
        await _orig_sleep(0)

    with (
        mock_patch(
            "custom_components.netatmo.data_handler.time",
            side_effect=lambda: _ha_dt_util.utcnow().timestamp(),
        ),
        mock_patch(
            "custom_components.netatmo.data_handler.asyncio.sleep",
            side_effect=_fast_sleep,
        ),
    ):
        yield


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
