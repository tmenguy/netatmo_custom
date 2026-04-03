"""Tests for Netatmo NLPC energy sensor entities."""

from __future__ import annotations

from collections.abc import Generator
from functools import partial
import json
from typing import Any
from unittest.mock import AsyncMock, patch

from pyatmo.modules.module import EnergyHistoryMixin
import pytest

from homeassistant.components.netatmo.const import DOMAIN
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import Platform, UnitOfEnergy
from homeassistant.core import HomeAssistant
from tests.common import MockConfigEntry, async_load_fixture
from tests.test_util.aiohttp import AiohttpClientMockResponse

from .common import fake_get_image, selected_platforms

PILOT_WIRE_HOME_ID = "aabbccddee000001"

# NLPC module IDs from homesdata_pilot_wire.json
NLPC_KITCHEN_ID = "12:34:56:80:02:01"
NLPC_LAUNDRY_ID = "12:34:56:80:02:02"

# Expected HA entity IDs (device name slug + description key)
ENTITY_KITCHEN_ENERGY = "sensor.energy_meter_kitchen_energy"
ENTITY_LAUNDRY_ENERGY = "sensor.energy_meter_laundry_energy"

# Expected HA entity IDs for power sensors
ENTITY_KITCHEN_POWER = "sensor.energy_meter_kitchen_power"
ENTITY_LAUNDRY_POWER = "sensor.energy_meter_laundry_power"


async def fake_post_request_energy(
    hass: HomeAssistant, *args: Any, **kwargs: Any
) -> Any:
    """Return fake data, loading pilot wire fixtures for energy tests."""
    if "endpoint" not in kwargs:
        return "{}"

    endpoint = kwargs["endpoint"].split("/")[-1]

    if endpoint in "snapshot_720.jpg":
        return b"test stream image bytes"

    if endpoint in [
        "setpersonsaway",
        "setpersonshome",
        "setstate",
        "setroomthermpoint",
        "setthermmode",
        "switchhomeschedule",
    ]:
        payload = {f"{endpoint}": True, "status": "ok"}

    elif endpoint in ("homesdata", "gethomedata"):
        payload = json.loads(
            await async_load_fixture(hass, "homesdata_pilot_wire.json", DOMAIN)
        )

    elif endpoint == "homestatus":
        home_id = kwargs.get("params", {}).get("home_id")
        payload = json.loads(
            await async_load_fixture(hass, f"{endpoint}_{home_id}.json", DOMAIN)
        )

    elif endpoint == "getmeasure":
        payload = json.loads(await async_load_fixture(hass, "getmeasure.json", DOMAIN))

    else:
        payload = json.loads(await async_load_fixture(hass, f"{endpoint}.json", DOMAIN))

    if "msg_callback" in kwargs:
        kwargs["msg_callback"](payload)

    return AiohttpClientMockResponse(
        method="POST",
        url=kwargs["endpoint"],
        json=payload,
    )


@pytest.fixture(name="netatmo_auth_energy")
def netatmo_auth_energy(hass: HomeAssistant) -> Generator[None]:
    """Patch auth to load pilot wire / energy fixtures."""
    with patch(
        "homeassistant.components.netatmo.api.AsyncConfigEntryNetatmoAuth"
    ) as mock_auth:
        mock_auth.return_value.async_post_request.side_effect = partial(
            fake_post_request_energy, hass
        )
        mock_auth.return_value.async_post_api_request.side_effect = partial(
            fake_post_request_energy, hass
        )
        mock_auth.return_value.async_get_image.side_effect = fake_get_image
        mock_auth.return_value.async_addwebhook.side_effect = AsyncMock()
        mock_auth.return_value.async_dropwebhook.side_effect = AsyncMock()
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_nlpc_device(hass: HomeAssistant, module_id: str) -> Any:
    """Get the pyatmo NLPC module object from the data handler."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        data_handler = entry.runtime_data
        for home in data_handler.account.homes.values():
            if module_id in home.modules:
                return home.modules[module_id]
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_energy_sensor_entity_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_energy: None,
) -> None:
    """Verify NLPC creates energy sensor entity with correct attributes."""
    with selected_platforms([Platform.SENSOR]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY_KITCHEN_ENERGY)
    assert state is not None, f"Entity {ENTITY_KITCHEN_ENERGY} not found"
    assert state.attributes["device_class"] == SensorDeviceClass.ENERGY
    assert state.attributes["state_class"] == SensorStateClass.TOTAL_INCREASING
    assert state.attributes["unit_of_measurement"] == UnitOfEnergy.WATT_HOUR

    state2 = hass.states.get(ENTITY_LAUNDRY_ENERGY)
    assert state2 is not None, f"Entity {ENTITY_LAUNDRY_ENERGY} not found"
    assert state2.attributes["device_class"] == SensorDeviceClass.ENERGY


async def test_energy_sensor_initial_state(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_energy: None,
) -> None:
    """Verify energy sensor has initial state from device data."""
    with selected_platforms([Platform.SENSOR]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY_KITCHEN_ENERGY)
    assert state is not None
    # The initial state is a numeric value (energy starts at 0 from reset_measures
    # and gets adapted by get_sum_energy_elec_power_adapted on first callback).
    # The value should be a valid number, not "unavailable" or "unknown".
    assert state.state not in ("unavailable", "unknown")
    val = float(state.state)
    assert val >= 0


async def test_energy_sensor_update_callback_normal(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_energy: None,
) -> None:
    """Verify update callback sets state from energy + delta."""
    with selected_platforms([Platform.SENSOR]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    device = _get_nlpc_device(hass, NLPC_KITCHEN_ID)
    assert device is not None
    assert isinstance(device, EnergyHistoryMixin)

    # Patch the device method to return controlled values
    with patch.object(
        device,
        "get_sum_energy_elec_power_adapted",
        return_value=(100.0, 5.0),
    ):
        device.in_reset = False
        # Find the entity and trigger callback
        entity_id = ENTITY_KITCHEN_ENERGY
        for entry in hass.config_entries.async_entries(DOMAIN):
            data_handler = entry.runtime_data
            # Emit callbacks for all publishers subscribed to this entity
            for publisher in data_handler.publisher.values():
                for cb in list(publisher.subscriptions):
                    if cb is not None:
                        cb()

        await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == 105.0


async def test_energy_sensor_monotonic_increasing(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_energy: None,
) -> None:
    """Verify energy value never decreases."""
    with selected_platforms([Platform.SENSOR]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    device = _get_nlpc_device(hass, NLPC_KITCHEN_ID)
    assert device is not None

    def _trigger_callbacks() -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            data_handler = entry.runtime_data
            for publisher in data_handler.publisher.values():
                for cb in list(publisher.subscriptions):
                    if cb is not None:
                        cb()

    # First update: energy = 100 + 5 = 105
    with patch.object(
        device,
        "get_sum_energy_elec_power_adapted",
        return_value=(100.0, 5.0),
    ):
        device.in_reset = False
        _trigger_callbacks()
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY_KITCHEN_ENERGY)
    assert state is not None
    assert float(state.state) == 105.0

    # Second update: lower value (100 - 10 + 3 = 93), should stay at 105
    with patch.object(
        device,
        "get_sum_energy_elec_power_adapted",
        return_value=(90.0, 3.0),
    ):
        device.in_reset = False
        _trigger_callbacks()
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY_KITCHEN_ENERGY)
    assert state is not None
    assert float(state.state) == 105.0


async def test_energy_sensor_increasing_value(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_energy: None,
) -> None:
    """Verify energy value increases when new reading is higher."""
    with selected_platforms([Platform.SENSOR]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    device = _get_nlpc_device(hass, NLPC_KITCHEN_ID)
    assert device is not None

    def _trigger_callbacks() -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            data_handler = entry.runtime_data
            for publisher in data_handler.publisher.values():
                for cb in list(publisher.subscriptions):
                    if cb is not None:
                        cb()

    # First update: 100 + 5 = 105
    with patch.object(
        device,
        "get_sum_energy_elec_power_adapted",
        return_value=(100.0, 5.0),
    ):
        device.in_reset = False
        _trigger_callbacks()
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY_KITCHEN_ENERGY)
    assert float(state.state) == 105.0

    # Second update: higher value (200 + 10 = 210), should update
    with patch.object(
        device,
        "get_sum_energy_elec_power_adapted",
        return_value=(200.0, 10.0),
    ):
        device.in_reset = False
        _trigger_callbacks()
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY_KITCHEN_ENERGY)
    assert float(state.state) == 210.0


async def test_energy_sensor_reset_flag(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_energy: None,
) -> None:
    """Verify in_reset flag resets energy to 0."""
    with selected_platforms([Platform.SENSOR]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    device = _get_nlpc_device(hass, NLPC_KITCHEN_ID)
    assert device is not None

    def _trigger_callbacks() -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            data_handler = entry.runtime_data
            for publisher in data_handler.publisher.values():
                for cb in list(publisher.subscriptions):
                    if cb is not None:
                        cb()

    # First set a non-zero value
    with patch.object(
        device,
        "get_sum_energy_elec_power_adapted",
        return_value=(100.0, 5.0),
    ):
        device.in_reset = False
        _trigger_callbacks()
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY_KITCHEN_ENERGY)
    assert float(state.state) == 105.0

    # Now trigger reset
    device.in_reset = True
    _trigger_callbacks()
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_KITCHEN_ENERGY)
    assert state is not None
    assert float(state.state) == 0.0


async def test_energy_sensor_reset_clears_in_reset_flag(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_energy: None,
) -> None:
    """Verify the in_reset flag is cleared after one 0-value callback."""
    with selected_platforms([Platform.SENSOR]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    device = _get_nlpc_device(hass, NLPC_KITCHEN_ID)
    assert device is not None

    def _trigger_callbacks() -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            data_handler = entry.runtime_data
            for publisher in data_handler.publisher.values():
                for cb in list(publisher.subscriptions):
                    if cb is not None:
                        cb()

    # Trigger reset
    device.in_reset = True
    _trigger_callbacks()
    await hass.async_block_till_done()

    # After the callback, in_reset should have been set to False
    assert device.in_reset is False


async def test_energy_sensor_power_attribute(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_energy: None,
) -> None:
    """Verify NLPC power reading from homestatus."""
    with selected_platforms([Platform.SENSOR]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    # The NLPC kitchen module has power=43 in homestatus_aabbccddee000001.json
    state = hass.states.get(ENTITY_KITCHEN_POWER)
    assert state is not None, f"Entity {ENTITY_KITCHEN_POWER} not found"
    assert state.state == "43"

    # The NLPC laundry module has power=0 in homestatus_aabbccddee000001.json
    state2 = hass.states.get(ENTITY_LAUNDRY_POWER)
    assert state2 is not None, f"Entity {ENTITY_LAUNDRY_POWER} not found"
    assert state2.state == "0"


async def test_energy_sensor_both_nlpc_devices(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_energy: None,
) -> None:
    """Verify both NLPC devices create energy sensor entities."""
    with selected_platforms([Platform.SENSOR]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    # Both NLPC devices should have energy entities
    kitchen = hass.states.get(ENTITY_KITCHEN_ENERGY)
    laundry = hass.states.get(ENTITY_LAUNDRY_ENERGY)

    assert kitchen is not None, "Kitchen NLPC energy entity missing"
    assert laundry is not None, "Laundry NLPC energy entity missing"

    # Both should be available
    assert kitchen.state != "unavailable"
    assert laundry.state != "unavailable"


async def test_energy_sensor_device_is_energy_history_mixin(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_energy: None,
) -> None:
    """Verify the underlying NLPC device is an EnergyHistoryMixin instance."""
    with selected_platforms([Platform.SENSOR]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    device = _get_nlpc_device(hass, NLPC_KITCHEN_ID)
    assert device is not None
    assert isinstance(device, EnergyHistoryMixin)
    assert hasattr(device, "sum_energy_elec")
    assert hasattr(device, "in_reset")
    assert hasattr(device, "get_sum_energy_elec_power_adapted")
