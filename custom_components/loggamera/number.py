"""Support for Loggamera heat pump setpoint (number) entities."""

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional, Set

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import LoggameraAPIError
from .const import (
    DOMAIN,
    EVENT_SETPOINT_WRITE_FAILED,
    PENDING_TIMEOUT_SECONDS,
    PROPERTY_SET_INDOOR_TEMP,
    WRITE_MIN_INTERVAL_SECONDS,
    WRITE_STATUS_CONFIRMED,
    WRITE_STATUS_EXPIRED,
    WRITE_STATUS_FAILED,
    WRITE_STATUS_SENDING,
    WRITE_STATUS_SENT,
)

_LOGGER = logging.getLogger(__name__)

# The heat pump applies a written setpoint after a few minutes, and the cloud data
# follows later, so refresh once after this delay to pick up the real value.
REFRESH_DELAY_SECONDS = 360


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up an indoor temperature setpoint for each writable heat pump."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    api = hass.data[DOMAIN][entry.entry_id]["api"]
    known: Set[str] = set()

    @callback
    def _add_new_setpoints() -> None:
        # Capabilities may only arrive on a later poll (e.g. a timeout on the first one)
        new = [
            device
            for device in coordinator.data.get("devices", [])
            if device.get("Class") == "HeatPump"
            and str(device["Id"]) not in known
            and PROPERTY_SET_INDOOR_TEMP
            in coordinator.write_capabilities.get(str(device["Id"]), [])
        ]
        if new:
            known.update(str(device["Id"]) for device in new)
            async_add_entities(LoggameraIndoorSetpoint(coordinator, api, device) for device in new)

    _add_new_setpoints()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_setpoints))


class LoggameraIndoorSetpoint(CoordinatorEntity, NumberEntity):
    """Indoor temperature setpoint of a Loggamera heat pump."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = 10.0
    _attr_native_max_value = 30.0
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:thermostat"

    def __init__(self, coordinator, api, device) -> None:
        """Initialize the setpoint entity."""
        super().__init__(coordinator)
        self._api = api
        self._device_id = device["Id"]
        self._device_type = device["Class"]
        self._device_name = device.get("Title", f"{self._device_type} {self._device_id}")
        self._attr_unique_id = f"loggamera_{self._device_id}_{PROPERTY_SET_INDOOR_TEMP}"
        self._attr_name = f"{self._device_name} Indoor Temperature Setpoint"
        # Value sent to the pump, shown until the device reports it or the deadline passes
        self._held_value: Optional[float] = None
        self._held_until = 0.0
        # Value waiting for WRITE_MIN_INTERVAL_SECONDS to pass, and the timer that sends it
        self._queued_value: Optional[float] = None
        self._queued_until: Optional[datetime] = None
        self._cancel_queued_write: Optional[Callable[[], None]] = None
        # The one refresh scheduled after the latest write
        self._cancel_refresh: Optional[Callable[[], None]] = None
        # Outcome of the last write, exposed as attributes for automations
        self._last_write_value: Optional[float] = None
        self._last_write_at: Optional[datetime] = None
        self._last_write_status: Optional[str] = None
        self._last_write_error: Optional[str] = None

    @property
    def device_info(self) -> DeviceInfo:
        """Attach to the same device as the heat pump's sensors."""
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._device_id))},
            name=f"{self._device_type} - {self._device_name}",
            manufacturer="Loggamera",
            model=f"Loggamera {self._device_type}",
        )

    def _reported_value(self) -> Optional[float]:
        """Return the setpoint the device last reported."""
        settings = self.coordinator.data.get("device_data", {}).get(
            f"settings_{self._device_id}", {}
        )
        value = settings.get(PROPERTY_SET_INDOOR_TEMP)
        return round(float(value), 1) if value is not None else None

    @property
    def native_value(self) -> Optional[float]:
        """Return the queued setpoint, else the sent and unreported one, else the reported one."""
        if self._queued_value is not None:
            return self._queued_value
        if self._held_value is not None:
            return self._held_value
        return self._reported_value()

    @property
    def available(self) -> bool:
        """Available while the heat pump settings were read successfully."""
        return self.native_value is not None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Expose what is queued, the last write and its outcome."""
        queued = self._queued_value is not None
        return {
            "reported_value": self._reported_value(),
            "queued_value": self._queued_value,
            "queued_until": self._queued_until.isoformat() if queued else None,
            "last_write_value": self._last_write_value,
            "last_write_at": self._last_write_at.isoformat() if self._last_write_at else None,
            "last_write_status": self._last_write_status,
            "last_write_error": self._last_write_error,
        }

    def _fire_write_failed(self, value: float, reason: str, error: str) -> None:
        """Let automations see a failed write, including queued ones nobody awaits."""
        self.hass.bus.async_fire(
            EVENT_SETPOINT_WRITE_FAILED,
            {
                "entity_id": self.entity_id,
                "loggamera_device_id": self._device_id,
                "value": value,
                "reason": reason,
                "error": error,
            },
        )

    def _update_write_status(self) -> None:
        """Mark the last write confirmed once reported back, or expired after the deadline."""
        if self._last_write_status != WRITE_STATUS_SENT:
            return
        if self._reported_value() == self._last_write_value:
            self._last_write_status = WRITE_STATUS_CONFIRMED
        elif time.monotonic() > self._held_until:
            self._last_write_status = WRITE_STATUS_EXPIRED
            error = f"Not reported back within {PENDING_TIMEOUT_SECONDS // 60} min"
            _LOGGER.warning(
                "Setpoint %s for device %s: %s", self._last_write_value, self._device_id, error
            )
            self._fire_write_failed(self._last_write_value, "not_confirmed", error)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Keep a sent value until the device reports it back or it expires."""
        self._update_write_status()
        if self._held_value is not None and self._last_write_status != WRITE_STATUS_SENDING:
            if self._reported_value() == self._held_value or time.monotonic() > self._held_until:
                self._held_value = None
        super()._handle_coordinator_update()

    def _cancel_queue(self) -> bool:
        """Drop a queued write; return True if there was one."""
        if self._cancel_queued_write is None:
            return False
        self._cancel_queued_write()
        self._cancel_queued_write = None
        self._queued_value = None
        self._queued_until = None
        return True

    async def async_will_remove_from_hass(self) -> None:
        """Drop a queued write and the pending refresh when the entity goes away."""
        if self._cancel_queue():
            _LOGGER.warning("Queued setpoint write for device %s was dropped", self._device_id)
        if self._cancel_refresh is not None:
            self._cancel_refresh()
            self._cancel_refresh = None
        await super().async_will_remove_from_hass()

    def _target_without_queue(self) -> Optional[float]:
        """Return where the pump ends up without the queued write: in-flight value or reported."""
        if self._last_write_status in (WRITE_STATUS_SENDING, WRITE_STATUS_SENT):
            return self._last_write_value
        return self._reported_value()

    def _skip_unchanged(self, value: float) -> bool:
        """Handle a value that changes nothing; return True if no write is needed."""
        if self._queued_value is not None and value == self._queued_value:
            _LOGGER.debug("Setpoint %.1f for device %s is already queued", value, self._device_id)
            return True
        if value != self._target_without_queue():
            return False
        if self._cancel_queue():
            # Back to where the pump is heading anyway, so the queued write is not needed
            _LOGGER.info(
                "Queued setpoint for device %s cancelled, now %.1f", self._device_id, value
            )
            self.async_write_ha_state()
        else:
            _LOGGER.debug("Setpoint %.1f for device %s is unchanged", value, self._device_id)
        return True

    async def async_set_native_value(self, value: float) -> None:
        """Write a new setpoint, or queue it if the last write was too recent."""
        # Sent with one decimal, so compare with one decimal too
        value = round(value, 1)
        if self._skip_unchanged(value):
            return
        last_write = self.coordinator.last_write.get(str(self._device_id))
        wait = 0.0
        if last_write is not None:
            wait = last_write + WRITE_MIN_INTERVAL_SECONDS - time.monotonic()

        if wait > 0:
            # The pump drops a write that closely follows another. Send only the newest
            # value once the interval has passed.
            if self._cancel_queued_write is not None:
                self._cancel_queued_write()
            self._queued_value = value
            self._cancel_queued_write = async_call_later(self.hass, wait, self._async_send_queued)
            self._queued_until = dt_util.utcnow() + timedelta(seconds=wait)
            _LOGGER.info(
                "Setpoint %.1f for device %s queued for %.0f s to space it from the previous write",
                value,
                self._device_id,
                wait,
            )
            self.async_write_ha_state()
            return

        # A queue whose timer has not fired yet is superseded by this direct write
        self._cancel_queue()
        await self._async_send(value)

    async def _async_send_queued(self, _now: datetime) -> None:
        """Send the newest queued value."""
        value = self._queued_value
        self._cancel_queued_write = None
        self._queued_value = None
        self._queued_until = None
        if value is None:
            return
        try:
            await self._async_send(value)
        except HomeAssistantError as err:
            # Nobody is awaiting a queued write, so log instead of raising
            _LOGGER.error("%s", err)

    async def _async_send(self, value: float) -> None:
        """Write the value to the heat pump."""
        # Stamped before the call: a failed or timed-out request may still have reached the pump
        self.coordinator.last_write[str(self._device_id)] = time.monotonic()
        self._last_write_value = value
        self._last_write_at = dt_util.utcnow()
        self._last_write_status = WRITE_STATUS_SENDING
        self._last_write_error = None
        # Hold the value from now, so a poll during the call does not show the old one
        self._held_value = value
        self._held_until = time.monotonic() + PENDING_TIMEOUT_SECONDS
        try:
            await self.hass.async_add_executor_job(
                self._api.set_property,
                self._device_id,
                PROPERTY_SET_INDOOR_TEMP,
                f"{value:.1f}",
            )
        except LoggameraAPIError as err:
            self._held_value = None
            self._last_write_status = WRITE_STATUS_FAILED
            self._last_write_error = str(err)
            self._fire_write_failed(value, "error", str(err))
            self.async_write_ha_state()
            raise HomeAssistantError(f"Could not set indoor temperature: {err}") from err

        _LOGGER.info("Set indoor temperature of device %s to %.1f", self._device_id, value)
        self._last_write_status = WRITE_STATUS_SENT
        self._held_until = time.monotonic() + PENDING_TIMEOUT_SECONDS
        self.async_write_ha_state()

        async def _refresh(_now: datetime) -> None:
            self._cancel_refresh = None
            await self.coordinator.async_request_refresh()

        if self._cancel_refresh is not None:
            self._cancel_refresh()
        self._cancel_refresh = async_call_later(self.hass, REFRESH_DELAY_SECONDS, _refresh)
