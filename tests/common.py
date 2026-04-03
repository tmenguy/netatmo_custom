"""Shim: re-export HA test common utilities with fixture path overrides.

Re-exports everything from pytest_homeassistant_custom_component.common so that
`from tests.common import MockConfigEntry` etc. works as in HA core tests.

Overrides get_fixture_path / load_fixture / async_load_fixture so fixture files
resolve relative to this repo's tests/ directory (not the phcc package directory).
"""

import functools
import pathlib

from pytest_homeassistant_custom_component.common import *  # noqa: F403
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry as MockConfigEntry,
    async_capture_events as async_capture_events,
    async_fire_time_changed as async_fire_time_changed,
    async_get_device_automations as async_get_device_automations,
    start_reauth_flow as start_reauth_flow,
)

from homeassistant.core import HomeAssistant

_TESTS_ROOT = pathlib.Path(__file__).parent


def get_fixture_path(filename: str, integration: str | None = None) -> pathlib.Path:
    """Get path of fixture, resolving relative to this repo's tests/ directory."""
    if integration is None:
        return _TESTS_ROOT / "fixtures" / filename
    return _TESTS_ROOT / "components" / integration / "fixtures" / filename


@functools.lru_cache
def load_fixture(filename: str, integration: str | None = None) -> str:
    """Load a fixture."""
    return get_fixture_path(filename, integration).read_text()


async def async_load_fixture(
    hass: HomeAssistant, filename: str, integration: str | None = None
) -> str:
    """Load a fixture (async)."""
    return await hass.async_add_executor_job(load_fixture, filename, integration)
