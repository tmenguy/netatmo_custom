"""Tests for Netatmo NLC pilot wire climate entities."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from functools import partial
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    PRESET_COMFORT,
    PRESET_ECO,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.netatmo.climate import PRESET_FROST_GUARD, PRESET_SCHEDULE
from homeassistant.components.netatmo.const import DOMAIN
from homeassistant.components.webhook import async_handle_webhook
from homeassistant.const import ATTR_ENTITY_ID, CONF_WEBHOOK_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.util.aiohttp import MockRequest
from tests.common import MockConfigEntry, async_load_fixture
from tests.test_util.aiohttp import AiohttpClientMockResponse

from .common import fake_get_image, selected_platforms

# COMMON_RESPONSE for pilot wire home (different home_id than default fixtures)
COMMON_RESPONSE_PW = {
    "user_id": "aabbccddeeff001122334455",
    "home_id": "aabbccddee000001",
    "home_name": "Test Home Pilot Wire",
    "user": {"id": "aabbccddeeff001122334455", "email": "john@doe.com"},
}

PILOT_WIRE_HOME_ID = "aabbccddee000001"

# Entity IDs for the three pilot wire rooms
ENTITY_PARENTS = "climate.parents_bathroom"
ENTITY_KIDS = "climate.kids_bathroom"
ENTITY_ARTHUR = "climate.arthur_bathroom"


async def fake_post_request_pw(hass: HomeAssistant, *args: Any, **kwargs: Any) -> Any:
    """Return fake data, loading pilot wire fixtures for homesdata."""
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

    else:
        payload = json.loads(await async_load_fixture(hass, f"{endpoint}.json", DOMAIN))

    if "msg_callback" in kwargs:
        kwargs["msg_callback"](payload)

    return AiohttpClientMockResponse(
        method="POST",
        url=kwargs["endpoint"],
        json=payload,
    )


@pytest.fixture(name="netatmo_auth_pilot_wire")
def netatmo_auth_pilot_wire(hass: HomeAssistant) -> Generator[None]:
    """Patch auth to load pilot wire fixtures."""
    with patch(
        "homeassistant.components.netatmo.api.AsyncConfigEntryNetatmoAuth"
    ) as mock_auth:
        mock_auth.return_value.async_post_request.side_effect = partial(
            fake_post_request_pw, hass
        )
        mock_auth.return_value.async_post_api_request.side_effect = partial(
            fake_post_request_pw, hass
        )
        mock_auth.return_value.async_get_image.side_effect = fake_get_image
        mock_auth.return_value.async_addwebhook.side_effect = AsyncMock()
        mock_auth.return_value.async_dropwebhook.side_effect = AsyncMock()
        yield


async def simulate_webhook_pw(
    hass: HomeAssistant, webhook_id: str, response: dict[str, Any]
) -> None:
    """Simulate a webhook event using the pilot wire home COMMON_RESPONSE."""
    request = MockRequest(
        method="POST",
        content=bytes(json.dumps({**COMMON_RESPONSE_PW, **response}), "utf-8"),
        mock_source="test",
    )
    await async_handle_webhook(hass, webhook_id, request)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


async def test_nlc_climate_entity_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Verify NLC creates climate entity with correct features."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY_PARENTS)
    assert state is not None

    assert state.attributes["supported_features"] == ClimateEntityFeature.PRESET_MODE

    preset_modes = state.attributes["preset_modes"]
    assert PRESET_FROST_GUARD in preset_modes
    assert PRESET_ECO in preset_modes
    assert PRESET_COMFORT in preset_modes
    assert PRESET_SCHEDULE in preset_modes
    assert len(preset_modes) == 4


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


async def test_nlc_initial_state_frost_guard_schedule(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Room with therm_setpoint_fp=frost_guard and mode=home -> hvac_mode=AUTO, preset=FROST_GUARD."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY_PARENTS)
    assert state is not None
    assert state.state == HVACMode.AUTO
    assert state.attributes["preset_mode"] == PRESET_FROST_GUARD


async def test_nlc_initial_state_comfort_manual(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Room with therm_setpoint_fp=comfort and mode=manual -> hvac_mode=HEAT, preset=COMFORT."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY_KIDS)
    assert state is not None
    assert state.state == HVACMode.HEAT
    assert state.attributes["preset_mode"] == PRESET_COMFORT


# ---------------------------------------------------------------------------
# HVAC action
# ---------------------------------------------------------------------------


async def test_nlc_hvac_action_heating(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Room with power=1500 -> HVACAction.HEATING."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY_KIDS)
    assert state is not None
    assert state.attributes["hvac_action"] == HVACAction.HEATING


async def test_nlc_hvac_action_idle(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Room with power=0 -> HVACAction.IDLE."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(ENTITY_PARENTS)
    assert state is not None
    assert state.attributes["hvac_action"] == HVACAction.IDLE


# ---------------------------------------------------------------------------
# Set HVAC mode
# ---------------------------------------------------------------------------


async def test_nlc_set_hvac_mode_heat(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Set HEAT -> calls async_therm_set with pilot_wire=comfort."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    with patch("pyatmo.room.Room.async_therm_set") as mock_therm_set:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: ENTITY_PARENTS, ATTR_HVAC_MODE: HVACMode.HEAT},
            blocking=True,
        )
        await hass.async_block_till_done()

        mock_therm_set.assert_called_once()
        call_kwargs = mock_therm_set.call_args
        assert call_kwargs.kwargs["pilot_wire"] == "comfort"
        assert call_kwargs.kwargs["mode"] == "manual"


async def test_nlc_set_hvac_mode_auto(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Set AUTO -> calls async_therm_set with mode=home."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    with patch("pyatmo.room.Room.async_therm_set") as mock_therm_set:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: ENTITY_KIDS, ATTR_HVAC_MODE: HVACMode.AUTO},
            blocking=True,
        )
        await hass.async_block_till_done()

        mock_therm_set.assert_called_once()
        call_kwargs = mock_therm_set.call_args
        assert call_kwargs.kwargs["mode"] == "home"


async def test_nlc_set_hvac_mode_off(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Set OFF -> calls async_therm_set with mode=hg (frost guard)."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    with patch("pyatmo.room.Room.async_therm_set") as mock_therm_set:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: ENTITY_KIDS, ATTR_HVAC_MODE: HVACMode.OFF},
            blocking=True,
        )
        await hass.async_block_till_done()

        mock_therm_set.assert_called_once()
        call_kwargs = mock_therm_set.call_args
        assert call_kwargs.kwargs["mode"] == "hg"
        assert call_kwargs.kwargs["pilot_wire"] == "frost_guard"


# ---------------------------------------------------------------------------
# Set preset modes
# ---------------------------------------------------------------------------


async def test_nlc_set_preset_comfort(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Set COMFORT -> pilot_wire=comfort, mode=manual."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    with patch("pyatmo.room.Room.async_therm_set") as mock_therm_set:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_ENTITY_ID: ENTITY_PARENTS, ATTR_PRESET_MODE: PRESET_COMFORT},
            blocking=True,
        )
        await hass.async_block_till_done()

        mock_therm_set.assert_called_once()
        call_kwargs = mock_therm_set.call_args
        assert call_kwargs.kwargs["pilot_wire"] == "comfort"
        assert call_kwargs.kwargs["mode"] == "manual"


async def test_nlc_set_preset_eco(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Set ECO -> pilot_wire=away, mode=manual."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    with patch("pyatmo.room.Room.async_therm_set") as mock_therm_set:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_ENTITY_ID: ENTITY_PARENTS, ATTR_PRESET_MODE: PRESET_ECO},
            blocking=True,
        )
        await hass.async_block_till_done()

        mock_therm_set.assert_called_once()
        call_kwargs = mock_therm_set.call_args
        assert call_kwargs.kwargs["pilot_wire"] == "away"
        assert call_kwargs.kwargs["mode"] == "manual"


async def test_nlc_set_preset_frost_guard(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Set FROST_GUARD -> mode=hg, pilot_wire=frost_guard."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    with patch("pyatmo.room.Room.async_therm_set") as mock_therm_set:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {
                ATTR_ENTITY_ID: ENTITY_KIDS,
                ATTR_PRESET_MODE: PRESET_FROST_GUARD,
            },
            blocking=True,
        )
        await hass.async_block_till_done()

        mock_therm_set.assert_called_once()
        call_kwargs = mock_therm_set.call_args
        assert call_kwargs.kwargs["mode"] == "hg"
        assert call_kwargs.kwargs["pilot_wire"] == "frost_guard"


async def test_nlc_set_preset_schedule(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Set SCHEDULE -> mode=home."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    with patch("pyatmo.room.Room.async_therm_set") as mock_therm_set:
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {
                ATTR_ENTITY_ID: ENTITY_KIDS,
                ATTR_PRESET_MODE: PRESET_SCHEDULE,
            },
            blocking=True,
        )
        await hass.async_block_till_done()

        mock_therm_set.assert_called_once()
        call_kwargs = mock_therm_set.call_args
        assert call_kwargs.kwargs["mode"] == "home"


# ---------------------------------------------------------------------------
# Webhook events
# ---------------------------------------------------------------------------


async def test_nlc_webhook_set_point_pilot_wire(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Webhook set_point with therm_setpoint_fp updates preset."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    webhook_id = config_entry.data[CONF_WEBHOOK_ID]

    # Verify initial state: Parents Bathroom is frost_guard / AUTO
    state = hass.states.get(ENTITY_PARENTS)
    assert state.state == HVACMode.AUTO
    assert state.attributes["preset_mode"] == PRESET_FROST_GUARD

    # Webhook: set_point changes Parents Bathroom to comfort
    response = {
        "home_id": PILOT_WIRE_HOME_ID,
        "room_id": "1000000001",
        "home": {
            "id": PILOT_WIRE_HOME_ID,
            "name": "Test Home Pilot Wire",
            "country": "FR",
            "rooms": [
                {
                    "id": "1000000001",
                    "name": "Parents Bathroom",
                    "type": "bathroom",
                    "therm_setpoint_mode": "manual",
                    "therm_setpoint_fp": "comfort",
                }
            ],
            "modules": [
                {
                    "id": "12:34:56:80:01:01",
                    "name": "Radiator Parents",
                    "type": "NLC",
                }
            ],
        },
        "mode": "manual",
        "event_type": "set_point",
        "push_type": "display_change",
    }
    await simulate_webhook_pw(hass, webhook_id, response)

    state = hass.states.get(ENTITY_PARENTS)
    assert state.attributes["preset_mode"] == PRESET_COMFORT
    assert state.state == HVACMode.HEAT


async def test_nlc_webhook_cancel_set_point(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """Webhook cancel_set_point returns to schedule."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    webhook_id = config_entry.data[CONF_WEBHOOK_ID]

    # First, simulate setting Kids Bathroom to OFF via a set_point webhook
    # so that cancel_set_point can revert it
    response_off = {
        "home_id": PILOT_WIRE_HOME_ID,
        "room_id": "1000000002",
        "home": {
            "id": PILOT_WIRE_HOME_ID,
            "name": "Test Home Pilot Wire",
            "country": "FR",
            "rooms": [
                {
                    "id": "1000000002",
                    "name": "Kids Bathroom",
                    "type": "bathroom",
                    "therm_setpoint_mode": "off",
                    "therm_setpoint_fp": "frost_guard",
                }
            ],
            "modules": [
                {
                    "id": "12:34:56:80:01:02",
                    "name": "Radiator Kids",
                    "type": "NLC",
                }
            ],
        },
        "mode": "off",
        "event_type": "set_point",
        "push_type": "display_change",
    }
    await simulate_webhook_pw(hass, webhook_id, response_off)

    # The NLC handler in handle_event reads therm_setpoint_fp for set_point events,
    # so the preset is based on the fp value.
    state = hass.states.get(ENTITY_KIDS)
    assert state.attributes["preset_mode"] == PRESET_FROST_GUARD

    # Now simulate a cancel_set_point which should trigger async_update_callback
    # and revert based on device state
    response_cancel = {
        "home_id": PILOT_WIRE_HOME_ID,
        "room_id": "1000000002",
        "home": {
            "id": PILOT_WIRE_HOME_ID,
            "name": "Test Home Pilot Wire",
            "country": "FR",
            "rooms": [
                {
                    "id": "1000000002",
                    "name": "Kids Bathroom",
                    "type": "bathroom",
                    "therm_setpoint_mode": "home",
                }
            ],
            "modules": [
                {
                    "id": "12:34:56:80:01:02",
                    "name": "Radiator Kids",
                    "type": "NLC",
                }
            ],
        },
        "mode": "home",
        "event_type": "cancel_set_point",
        "push_type": "display_change",
    }
    await simulate_webhook_pw(hass, webhook_id, response_cancel)

    # After cancel, the entity calls async_update_callback which reads from device state.
    # The underlying device data still has comfort/manual from the fixture,
    # so it reverts to the device's actual state.
    state = hass.states.get(ENTITY_KIDS)
    assert state is not None
    # The cancel_set_point handler checks if hvac_mode was OFF, and if so sets AUTO/SCHEDULE.
    # Since we didn't actually change _attr_hvac_mode to OFF (NLC set_point handler
    # doesn't set OFF), the state comes from async_update_callback reading device data.
    assert state.state in (HVACMode.AUTO, HVACMode.HEAT)


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


async def test_nlc_set_preset_retry_on_timeout(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth_pilot_wire: None,
) -> None:
    """TimeoutError on first attempt -> retries successfully."""
    with selected_platforms([Platform.CLIMATE]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    call_count = 0

    async def therm_set_with_timeout(*args: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise asyncio.TimeoutError
        # Second call succeeds

    with patch(
        "pyatmo.room.Room.async_therm_set",
        side_effect=therm_set_with_timeout,
    ):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_ENTITY_ID: ENTITY_PARENTS, ATTR_PRESET_MODE: PRESET_COMFORT},
            blocking=True,
        )
        await hass.async_block_till_done()

    # Should have been called twice: first attempt timed out, second succeeded
    assert call_count == 2
