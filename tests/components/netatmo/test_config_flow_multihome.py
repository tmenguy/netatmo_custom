"""Tests for multi-home selection in the Netatmo options flow."""

from __future__ import annotations

from collections.abc import Generator
from functools import partial
import json
from typing import Any
from unittest.mock import AsyncMock, patch

from pyatmo.const import ALL_SCOPES
import pytest

from homeassistant.components.netatmo.config_flow import INTERMEDIATE_ENABLED_HOMES
from homeassistant.components.netatmo.const import (
    CONF_DISABLED_HOMES,
    CONF_WEATHER_AREAS,
    DOMAIN,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from tests.common import MockConfigEntry, async_load_fixture
from tests.test_util.aiohttp import AiohttpClientMockResponse

from .common import selected_platforms

CLIENT_ID = "1234"

HOME_ID_1 = "aabbccddee000001"
HOME_ID_2 = "aabbccddee000002"
HOME_ID_3 = "aabbccddee000003"
ALL_HOME_IDS = [HOME_ID_1, HOME_ID_2, HOME_ID_3]


async def fake_post_request_multihome(
    hass: HomeAssistant, *args: Any, **kwargs: Any
) -> AiohttpClientMockResponse | str | bytes:
    """Return fake data using multi-home fixtures."""
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

    elif endpoint == "homestatus":
        home_id = kwargs.get("params", {}).get("home_id")
        payload = json.loads(
            await async_load_fixture(hass, f"{endpoint}_{home_id}.json", DOMAIN)
        )

    elif endpoint in ("homesdata", "gethomedata"):
        payload = json.loads(
            await async_load_fixture(hass, "homesdata_multihome.json", DOMAIN)
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


@pytest.fixture(name="netatmo_auth_multihome")
def netatmo_auth_multihome(hass: HomeAssistant) -> Generator[None]:
    """Provide auth mock that serves multi-home fixture data."""
    with patch(
        "homeassistant.components.netatmo.api.AsyncConfigEntryNetatmoAuth"
    ) as mock_auth:
        mock_auth.return_value.async_post_request.side_effect = partial(
            fake_post_request_multihome, hass
        )
        mock_auth.return_value.async_post_api_request.side_effect = partial(
            fake_post_request_multihome, hass
        )
        mock_auth.return_value.async_get_image.side_effect = AsyncMock()
        mock_auth.return_value.async_addwebhook.side_effect = AsyncMock()
        mock_auth.return_value.async_dropwebhook.side_effect = AsyncMock()
        yield


def _make_config_entry(
    hass: HomeAssistant,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Create a Netatmo config entry with the given options."""
    if options is None:
        options = {CONF_WEATHER_AREAS: {}}
    mock_entry = MockConfigEntry(
        domain="netatmo",
        data={
            "auth_implementation": "cloud",
            "token": {
                "refresh_token": "mock-refresh-token",
                "access_token": "mock-access-token",
                "type": "Bearer",
                "expires_in": 60,
                "expires_at": 1e10,
                "scope": ALL_SCOPES,
            },
        },
        options=options,
    )
    mock_entry.add_to_hass(hass)
    return mock_entry


async def _setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Set up the Netatmo integration so runtime_data is populated."""
    with selected_platforms([]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()


async def test_option_flow_shows_home_selector_multihome(
    hass: HomeAssistant,
    netatmo_auth_multihome: None,
) -> None:
    """Options flow shows home multi-select when more than one home exists."""
    config_entry = _make_config_entry(hass)
    await _setup_integration(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "public_weather_areas_and_homes"

    schema_keys = [str(k) for k in result["data_schema"].schema]
    assert INTERMEDIATE_ENABLED_HOMES in schema_keys


async def test_option_flow_hides_home_selector_single_home(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth: None,
) -> None:
    """Options flow has no home selector when only a single home exists."""
    await _setup_integration(hass, config_entry)

    # The default fixture has 2 homes. Override all_homes_id to simulate a
    # single-home account so the selector branch is skipped.
    config_entry.runtime_data.account.all_homes_id = {
        "91763b24c43d3e344f424e8b": "MYHOME",
    }

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "public_weather_areas_and_homes"

    schema_keys = [str(k) for k in result["data_schema"].schema]
    assert INTERMEDIATE_ENABLED_HOMES not in schema_keys


async def test_option_flow_disable_one_home(
    hass: HomeAssistant,
    netatmo_auth_multihome: None,
) -> None:
    """Deselecting a home stores it in CONF_DISABLED_HOMES."""
    config_entry = _make_config_entry(hass)
    await _setup_integration(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM

    # Select only the first two homes, leaving the third disabled.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            INTERMEDIATE_ENABLED_HOMES: [HOME_ID_1, HOME_ID_2],
            CONF_WEATHER_AREAS: [],
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_DISABLED_HOMES] == [HOME_ID_3]


async def test_option_flow_enable_all_homes(
    hass: HomeAssistant,
    netatmo_auth_multihome: None,
) -> None:
    """Selecting all homes results in empty CONF_DISABLED_HOMES."""
    config_entry = _make_config_entry(
        hass,
        options={
            CONF_WEATHER_AREAS: {},
            CONF_DISABLED_HOMES: [HOME_ID_3],
        },
    )
    await _setup_integration(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM

    # Select all three homes.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            INTERMEDIATE_ENABLED_HOMES: ALL_HOME_IDS,
            CONF_WEATHER_AREAS: [],
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_DISABLED_HOMES] == []


async def test_option_flow_preserves_weather_areas(
    hass: HomeAssistant,
    netatmo_auth_multihome: None,
) -> None:
    """Weather areas survive when changing home selection."""
    weather_area_data = {
        "Home avg": {
            "lat_ne": 32.2345678,
            "lon_ne": -117.1234567,
            "lat_sw": 32.1234567,
            "lon_sw": -117.2345678,
            "show_on_map": False,
            "area_name": "Home avg",
            "mode": "avg",
        },
    }
    config_entry = _make_config_entry(
        hass,
        options={
            CONF_WEATHER_AREAS: weather_area_data,
        },
    )
    await _setup_integration(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM

    # Disable one home, but keep the weather area selected.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            INTERMEDIATE_ENABLED_HOMES: [HOME_ID_1, HOME_ID_2],
            CONF_WEATHER_AREAS: ["Home avg"],
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "Home avg" in config_entry.options[CONF_WEATHER_AREAS]
    assert (
        config_entry.options[CONF_WEATHER_AREAS]["Home avg"]
        == weather_area_data["Home avg"]
    )
    assert config_entry.options[CONF_DISABLED_HOMES] == [HOME_ID_3]


async def test_disabled_homes_stored_in_options(
    hass: HomeAssistant,
    netatmo_auth_multihome: None,
) -> None:
    """Verify CONF_DISABLED_HOMES is persisted in config entry options."""
    config_entry = _make_config_entry(hass)
    await _setup_integration(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    # Disable two of three homes.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            INTERMEDIATE_ENABLED_HOMES: [HOME_ID_1],
            CONF_WEATHER_AREAS: [],
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    disabled = config_entry.options[CONF_DISABLED_HOMES]
    assert HOME_ID_2 in disabled
    assert HOME_ID_3 in disabled
    assert HOME_ID_1 not in disabled
    assert len(disabled) == 2


async def test_option_flow_default_selects_all_homes(
    hass: HomeAssistant,
    netatmo_auth_multihome: None,
) -> None:
    """First-time options flow defaults to all homes enabled."""
    config_entry = _make_config_entry(hass)
    await _setup_integration(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM

    # Inspect the schema default for the home selector.
    for key in result["data_schema"].schema:
        if str(key) == INTERMEDIATE_ENABLED_HOMES:
            assert sorted(key.default()) == sorted(ALL_HOME_IDS)
            break
    else:
        pytest.fail(f"{INTERMEDIATE_ENABLED_HOMES} not found in form schema")


async def test_option_flow_defaults_reflect_disabled_homes(
    hass: HomeAssistant,
    netatmo_auth_multihome: None,
) -> None:
    """Options flow defaults exclude previously disabled homes."""
    config_entry = _make_config_entry(
        hass,
        options={
            CONF_WEATHER_AREAS: {},
            CONF_DISABLED_HOMES: [HOME_ID_2],
        },
    )
    await _setup_integration(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM

    for key in result["data_schema"].schema:
        if str(key) == INTERMEDIATE_ENABLED_HOMES:
            defaults = key.default()
            assert HOME_ID_1 in defaults
            assert HOME_ID_3 in defaults
            assert HOME_ID_2 not in defaults
            break
    else:
        pytest.fail(f"{INTERMEDIATE_ENABLED_HOMES} not found in form schema")
