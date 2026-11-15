"""Tests for the heat pump setpoint (write) support."""

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.loggamera.api import LoggameraAPI, LoggameraAPIError
from custom_components.loggamera.const import (
    API_URL_V1,
    CONF_API_KEY,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    DOMAIN,
    EVENT_SETPOINT_WRITE_FAILED,
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
    assert entity._held_value is None
    assert _state(hass) == 21.5


async def test_written_value_expires_if_never_reported(hass, api_mocks):
    """A write the pump never applied stops being shown after the deadline."""
    coordinator = await _setup(hass)
    with patch(f"{NUMBER}.async_call_later"):
        await _set(hass, 21.5)
    hass.data["number"].get_entity(ENTITY)._held_until = 0  # deadline passed

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


def _attrs(hass):
    return hass.states.get(ENTITY).attributes


def _capture_timers():
    timers = []

    def _capture(_hass, delay, action):
        timers.append(action)
        return MagicMock()

    return timers, _capture


async def test_attributes_follow_a_write_until_confirmed(hass, api_mocks):
    coordinator = await _setup(hass)
    assert _attrs(hass)["reported_value"] == 21.0
    assert _attrs(hass)["last_write_status"] is None

    with patch(f"{NUMBER}.async_call_later"):
        await _set(hass, 21.5)
    attrs = _attrs(hass)
    assert attrs["last_write_status"] == "sent"
    assert attrs["last_write_value"] == 21.5
    assert attrs["last_write_at"] is not None
    assert attrs["queued_value"] is None

    await coordinator.async_refresh()  # not applied yet
    await hass.async_block_till_done()
    assert _attrs(hass)["last_write_status"] == "sent"

    api_mocks["get_heat_pump_settings"].return_value = {"SetIndoorTemp": 21.5}
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _attrs(hass)["last_write_status"] == "confirmed"
    assert _attrs(hass)["reported_value"] == 21.5


async def test_unconfirmed_write_expires_and_fires_event(hass, api_mocks):
    coordinator = await _setup(hass)
    events = async_capture_events(hass, EVENT_SETPOINT_WRITE_FAILED)
    with patch(f"{NUMBER}.async_call_later"):
        await _set(hass, 21.5)
    hass.data["number"].get_entity(ENTITY)._held_until = 0  # deadline passed

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _attrs(hass)["last_write_status"] == "expired"
    assert len(events) == 1
    assert events[0].data["reason"] == "not_confirmed"
    assert events[0].data["value"] == 21.5
    assert events[0].data["entity_id"] == ENTITY
    assert events[0].data["loggamera_device_id"] == 1234

    await coordinator.async_refresh()  # fired once, not on every later poll
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_immediate_failed_write_fires_event_and_sets_status(hass, api_mocks):
    await _setup(hass)
    events = async_capture_events(hass, EVENT_SETPOINT_WRITE_FAILED)
    api_mocks["set_property"].side_effect = LoggameraAPIError("rejected")
    with pytest.raises(HomeAssistantError):
        await _set(hass, 22.0)
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data["reason"] == "error"
    assert _attrs(hass)["last_write_status"] == "failed"
    assert "rejected" in _attrs(hass)["last_write_error"]


async def test_queued_write_shows_attributes_and_reports_later_failure(hass, api_mocks):
    """set_value returns at once for a queued write; its later failure must be visible."""
    await _setup(hass)
    events = async_capture_events(hass, EVENT_SETPOINT_WRITE_FAILED)
    timers, capture = _capture_timers()
    with patch(f"{NUMBER}.async_call_later", side_effect=capture):
        await _set(hass, 21.5)  # sent now
        await _set(hass, 22.0)  # queued, returns without error
    attrs = _attrs(hass)
    assert attrs["queued_value"] == 22.0
    assert attrs["queued_until"] is not None
    assert attrs["last_write_value"] == 21.5

    api_mocks["set_property"].side_effect = LoggameraAPIError("rejected")
    send_queued = [t for t in timers if t.__name__ == "_async_send_queued"][-1]
    with patch(f"{NUMBER}.async_call_later"):
        await send_queued(None)  # logs instead of raising
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["value"] == 22.0
    assert events[0].data["reason"] == "error"
    attrs = _attrs(hass)
    assert attrs["last_write_status"] == "failed"
    assert attrs["last_write_value"] == 22.0
    assert attrs["queued_value"] is None
    assert attrs["queued_until"] is None
    assert _state(hass) == 21.0  # back to what the pump reports


async def test_value_the_pump_already_reports_is_not_written(hass, api_mocks):
    await _setup(hass)
    await _set(hass, 21.0)
    api_mocks["set_property"].assert_not_called()
    assert _attrs(hass)["last_write_status"] is None


async def test_resending_an_unconfirmed_value_is_skipped(hass, api_mocks):
    await _setup(hass)
    timers, capture = _capture_timers()
    with patch(f"{NUMBER}.async_call_later", side_effect=capture):
        await _set(hass, 21.5)  # sent, pump still reports 21.0
        await _set(hass, 21.5)  # e.g. a retry or a second button press
    assert api_mocks["set_property"].call_count == 1
    assert not [t for t in timers if t.__name__ == "_async_send_queued"]


async def test_reported_value_is_written_while_another_write_is_in_flight(hass, api_mocks):
    """Pump reports 21.0 but is heading to 21.5, so asking for 21.0 must not be skipped."""
    await _setup(hass)
    timers, capture = _capture_timers()
    with patch(f"{NUMBER}.async_call_later", side_effect=capture):
        await _set(hass, 21.5)
        await _set(hass, 21.0)
    assert _attrs(hass)["queued_value"] == 21.0
    assert [t for t in timers if t.__name__ == "_async_send_queued"]


async def test_value_already_queued_is_not_queued_again(hass, api_mocks):
    await _setup(hass)
    timers, capture = _capture_timers()
    with patch(f"{NUMBER}.async_call_later", side_effect=capture):
        await _set(hass, 21.5)
        await _set(hass, 22.0)
        await _set(hass, 22.0)
    assert len([t for t in timers if t.__name__ == "_async_send_queued"]) == 1
    assert _attrs(hass)["queued_value"] == 22.0


async def test_going_back_to_the_in_flight_value_cancels_the_queued_write(hass, api_mocks):
    await _setup(hass)
    cancel = MagicMock()
    timers = []

    def _capture(_hass, delay, action):
        timers.append(action)
        return cancel if action.__name__ == "_async_send_queued" else MagicMock()

    with patch(f"{NUMBER}.async_call_later", side_effect=_capture):
        await _set(hass, 21.5)  # sent
        await _set(hass, 22.0)  # queued
        await _set(hass, 21.5)  # where the pump is heading anyway
    cancel.assert_called_once()
    assert api_mocks["set_property"].call_count == 1
    assert _attrs(hass)["queued_value"] is None
    assert _state(hass) == 21.5


async def test_failed_write_keeps_a_value_queued_meanwhile(hass, api_mocks):
    """A value queued while a queued write is in flight is still sent if that write fails."""
    await _setup(hass)
    entity = hass.data["number"].get_entity(ENTITY)
    timers, capture = _capture_timers()

    def _queue_newer_then_fail(*_args):
        # What a set_value arriving during the call does (it is queued: last_write is recent)
        entity._queued_value = 22.5
        entity._queued_until = entity._last_write_at
        entity._cancel_queued_write = MagicMock()
        raise LoggameraAPIError("rejected")

    with patch(f"{NUMBER}.async_call_later", side_effect=capture):
        await _set(hass, 21.5)  # sent now
        await _set(hass, 22.0)  # queued
        api_mocks["set_property"].side_effect = _queue_newer_then_fail
        send_queued = [t for t in timers if t.__name__ == "_async_send_queued"][-1]
        await send_queued(None)  # sends 22.0, which fails; logs instead of raising
    assert _attrs(hass)["queued_value"] == 22.5
    assert _state(hass) == 22.5

    api_mocks["set_property"].side_effect = None
    with patch(f"{NUMBER}.async_call_later"):
        await entity._async_send_queued(None)
    api_mocks["set_property"].assert_called_with(1234, "SetIndoorTemp", "22.5")


async def test_poll_during_a_write_keeps_the_new_value_and_status(hass, api_mocks):
    """A poll landing while the write is in flight neither shows the old value nor judges it."""
    coordinator = await _setup(hass)
    events = async_capture_events(hass, EVENT_SETPOINT_WRITE_FAILED)
    entity = hass.data["number"].get_entity(ENTITY)
    seen = {}

    def _poll_mid_call(*_args):
        entity._handle_coordinator_update()  # pump still reports 21.0
        seen["state"] = entity.native_value
        seen["status"] = entity._last_write_status

    api_mocks["set_property"].side_effect = _poll_mid_call
    with patch(f"{NUMBER}.async_call_later"):
        await _set(hass, 21.5)
    await hass.async_block_till_done()
    assert seen == {"state": 21.5, "status": "sending"}
    assert _attrs(hass)["last_write_status"] == "sent"
    assert _state(hass) == 21.5
    assert not events


async def test_only_one_refresh_timer_is_kept_after_writes(hass, api_mocks):
    await _setup(hass)
    refresh_cancels = []

    def _capture(_hass, delay, action):
        cancel = MagicMock()
        if action.__name__ == "_refresh":
            refresh_cancels.append(cancel)
        return cancel

    entity = hass.data["number"].get_entity(ENTITY)
    with patch(f"{NUMBER}.async_call_later", side_effect=_capture):
        await _set(hass, 21.5)
        entity.coordinator.last_write.clear()  # pretend the interval passed
        await _set(hass, 22.0)
    assert len(refresh_cancels) == 2
    refresh_cancels[0].assert_called_once()  # replaced by the second write's refresh
    refresh_cancels[1].assert_not_called()
    await entity.async_will_remove_from_hass()
    refresh_cancels[1].assert_called_once()


async def test_value_is_compared_with_one_decimal(hass, api_mocks):
    """22.04 is sent as "22.0", so a pump reporting 22.0 already has it."""
    await _setup(hass)
    api_mocks["get_heat_pump_settings"].return_value = {"SetIndoorTemp": 21.0}
    await _set(hass, 21.04)
    api_mocks["set_property"].assert_not_called()
