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
    WRITE_MIN_INTERVAL_SECONDS,
)

VALUES = {"Data": {"Values": [{"Name": "x", "Value": "1", "ValueType": "DECIMAL"}]}}
API = "custom_components.loggamera.LoggameraAPI"
NUMBER = "custom_components.loggamera.number"
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


@pytest.fixture
def api_mocks():
    """Patch the API calls made during setup and polling; tests can change return values."""
    mocks = {}
    patches = {
        "get_device_data": {"return_value": VALUES},
        "get_raw_data": {"return_value": {"Data": {"Values": []}}},
        "get_write_capabilities": {"return_value": ["SetIndoorTemp"]},
        "get_heat_pump_settings": {"return_value": {"SetIndoorTemp": 21.0}},
        "set_property": {},
    }
    for name, kwargs in patches.items():
        mocks[name] = patch(f"{API}.{name}", **kwargs).start()
    yield mocks
    patch.stopall()


async def _setup(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="device_1234",
        data={CONF_API_KEY: "test-key", CONF_DEVICE_ID: 1234, CONF_DEVICE_TYPE: "HeatPump"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]


async def _set(hass, value):
    await hass.services.async_call(
        "number", "set_value", {"entity_id": ENTITY, "value": value}, blocking=True
    )


def _state(hass):
    return float(hass.states.get(ENTITY).state)


async def test_setpoint_entity_reads_value(hass, api_mocks):
    await _setup(hass)
    assert _state(hass) == 21.0


async def test_no_setpoint_entity_without_write_capability(hass, api_mocks):
    api_mocks["get_write_capabilities"].return_value = ["SetHotWaterTemp"]
    await _setup(hass)
    assert hass.states.get(ENTITY) is None


async def test_setting_value_writes_string_and_shows_it(hass, api_mocks):
    await _setup(hass)
    with patch(f"{NUMBER}.async_call_later"):
        await _set(hass, 21.5)
    api_mocks["set_property"].assert_called_once_with(1234, "SetIndoorTemp", "21.5")
    assert _state(hass) == 21.5


async def test_rejected_write_raises_and_keeps_value(hass, api_mocks):
    await _setup(hass)
    api_mocks["set_property"].side_effect = LoggameraAPIError("rejected")
    with pytest.raises(HomeAssistantError):
        await _set(hass, 22.0)
    assert _state(hass) == 21.0


async def test_written_value_survives_polls_until_reported(hass, api_mocks):
    """A poll before the pump applied the write must not flick the state back."""
    coordinator = await _setup(hass)
    with patch(f"{NUMBER}.async_call_later"):
        await _set(hass, 21.5)

    await coordinator.async_refresh()  # pump still reports 21.0
    await hass.async_block_till_done()
    assert _state(hass) == 21.5

    api_mocks["get_heat_pump_settings"].return_value = {"SetIndoorTemp": 21.5}
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    entity = hass.data["number"].get_entity(ENTITY)
    assert entity._pending_value is None
    assert _state(hass) == 21.5


async def test_written_value_expires_if_never_reported(hass, api_mocks):
    """A write the pump never applied stops being shown after the deadline."""
    coordinator = await _setup(hass)
    with patch(f"{NUMBER}.async_call_later"):
        await _set(hass, 21.5)
    hass.data["number"].get_entity(ENTITY)._pending_until = 0  # deadline passed

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _state(hass) == 21.0


async def test_quick_second_write_is_queued_and_only_newest_sent(hass, api_mocks):
    """Writes closer than the minimum interval are coalesced, never sent back to back."""
    await _setup(hass)
    timers = []

    def _capture(_hass, delay, action):
        timers.append((delay, action))
        return MagicMock()

    with patch(f"{NUMBER}.async_call_later", side_effect=_capture):
        await _set(hass, 21.5)  # sent now
        await _set(hass, 22.0)  # queued
        await _set(hass, 22.5)  # replaces the queued value
    assert api_mocks["set_property"].call_count == 1
    assert _state(hass) == 22.5

    queued = [
        (delay, action) for delay, action in timers if action.__name__ == "_async_send_queued"
    ]
    assert len(queued) == 2  # one per queued write, the first was cancelled
    assert queued[-1][0] <= WRITE_MIN_INTERVAL_SECONDS

    with patch(f"{NUMBER}.async_call_later"):
        await queued[-1][1](None)
    assert api_mocks["set_property"].call_count == 2
    api_mocks["set_property"].assert_called_with(1234, "SetIndoorTemp", "22.5")


async def test_setpoint_added_when_capabilities_arrive_later(hass, api_mocks):
    """A failed first capabilities lookup must not hide the entity until a reload."""
    api_mocks["get_write_capabilities"].side_effect = LoggameraAPIError("timeout")
    coordinator = await _setup(hass)
    assert hass.states.get(ENTITY) is None

    api_mocks["get_write_capabilities"].side_effect = None
    coordinator._capabilities_retry_at.clear()  # the retry delay has passed
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _state(hass) == 21.0


async def test_failed_capabilities_lookup_is_not_retried_every_poll(hass, api_mocks):
    api_mocks["get_write_capabilities"].side_effect = LoggameraAPIError("Invalid ApiKey")
    coordinator = await _setup(hass)
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert api_mocks["get_write_capabilities"].call_count == 1
