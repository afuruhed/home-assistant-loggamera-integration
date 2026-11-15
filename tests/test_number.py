"""Tests for the heat pump setpoint (write) support."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.loggamera.api import LoggameraAPI, LoggameraAPIError
from custom_components.loggamera.const import (
    API_URL_V1,
    CONF_API_KEY,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    DOMAIN,
)

VALUES = {"Data": {"Values": [{"Name": "x", "Value": "1", "ValueType": "DECIMAL"}]}}
API = "custom_components.loggamera.LoggameraAPI"
ENTITY = "number.heatpump_1234_indoor_temperature_setpoint"


def _response(payload):
    response = MagicMock(status_code=200, text="x")
    response.json.return_value = payload
    return response


def test_set_property_posts_to_v1_with_string_value():
    api = LoggameraAPI("key")
    with patch.object(
        api.session, "post", return_value=_response({"Data": {"Result": True}})
    ) as post:
        api.set_property(1234, "SetIndoorTemp", "21.5")
    url = post.call_args.args[0]
    assert url == f"{API_URL_V1}/SetProperty"
    assert post.call_args.kwargs["json"] == {
        "DeviceId": 1234,
        "PropertyName": "SetIndoorTemp",
        "Value": "21.5",
        "ApiKey": "key",
    }


def test_set_property_raises_when_rejected():
    api = LoggameraAPI("key")
    with patch.object(api.session, "post", return_value=_response({"Data": {"Result": False}})):
        with pytest.raises(LoggameraAPIError):
            api.set_property(1234, "SetIndoorTemp", "21.5")


def test_get_write_capabilities_and_settings():
    api = LoggameraAPI("key")
    caps = {"Data": {"WriteCapabilities": ["SetIndoorTemp"]}, "Error": None}
    with patch.object(api.session, "post", return_value=_response(caps)):
        assert api.get_write_capabilities(1234) == ["SetIndoorTemp"]
    settings = {"Data": {"SetIndoorTemp": 21.0}, "Error": None}
    with patch.object(api.session, "post", return_value=_response(settings)) as post:
        assert api.get_heat_pump_settings(1234) == {"SetIndoorTemp": 21.0}
    assert post.call_args.args[0] == f"{API_URL_V1}/HeatPump"


async def _setup(hass, write_capabilities):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="device_1234",
        data={CONF_API_KEY: "test-key", CONF_DEVICE_ID: 1234, CONF_DEVICE_TYPE: "HeatPump"},
    )
    entry.add_to_hass(hass)
    patches = [
        patch(f"{API}.get_device_data", return_value=VALUES),
        patch(f"{API}.get_raw_data", return_value={"Data": {"Values": []}}),
        patch(f"{API}.get_write_capabilities", return_value=write_capabilities),
        patch(f"{API}.get_heat_pump_settings", return_value={"SetIndoorTemp": 21.0}),
    ]
    for p in patches:
        p.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry, patches


async def test_setpoint_entity_reads_value(hass):
    _, patches = await _setup(hass, ["SetIndoorTemp"])
    try:
        assert float(hass.states.get(ENTITY).state) == 21.0
    finally:
        patch.stopall()


async def test_no_setpoint_entity_without_write_capability(hass):
    _, _ = await _setup(hass, ["SetHotWaterTemp"])
    try:
        assert hass.states.get(ENTITY) is None
    finally:
        patch.stopall()


async def test_setting_value_writes_string_and_shows_it(hass):
    await _setup(hass, ["SetIndoorTemp"])
    try:
        with (
            patch(f"{API}.set_property") as set_property,
            patch("custom_components.loggamera.number.async_call_later"),
        ):
            await hass.services.async_call(
                "number", "set_value", {"entity_id": ENTITY, "value": 21.5}, blocking=True
            )
        set_property.assert_called_once_with(1234, "SetIndoorTemp", "21.5")
        assert float(hass.states.get(ENTITY).state) == 21.5
    finally:
        patch.stopall()


async def test_rejected_write_raises_and_keeps_value(hass):
    await _setup(hass, ["SetIndoorTemp"])
    try:
        with patch(f"{API}.set_property", side_effect=LoggameraAPIError("rejected")):
            with pytest.raises(HomeAssistantError):
                await hass.services.async_call(
                    "number", "set_value", {"entity_id": ENTITY, "value": 22.0}, blocking=True
                )
        assert float(hass.states.get(ENTITY).state) == 21.0
    finally:
        patch.stopall()
