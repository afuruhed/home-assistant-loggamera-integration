"""Support for Loggamera heat pump setpoint (number) entities."""

import logging
import time
from datetime import datetime
from typing import Callable, Optional, Set

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import LoggameraAPIError
from .const import (
    DOMAIN,
    PENDING_TIMEOUT_SECONDS,
    PROPERTY_SET_INDOOR_TEMP,
    WRITE_MIN_INTERVAL_SECONDS,
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
        # Value written (or queued), shown until the device reports it or the deadline passes
        self._pending_value: Optional[float] = None
        self._pending_until = 0.0
        # Cancels the timer of a write waiting for WRITE_MIN_INTERVAL_SECONDS to pass
        self._cancel_queued_write: Optional[Callable[[], None]] = None

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
        return float(value) if value is not None else None

    @property
    def native_value(self) -> Optional[float]:
        """Return the pending setpoint if there is one, else the reported one."""
        if self._pending_value is not None:
            return self._pending_value
        return self._reported_value()

    @property
    def available(self) -> bool:
        """Available while the heat pump settings were read successfully."""
        return self.native_value is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Keep a pending value until the device reports it back or it expires."""
        # A queued value has not been sent yet, so the device cannot have confirmed it
        if self._pending_value is not None and self._cancel_queued_write is None:
            reported = self._reported_value()
            if reported == self._pending_value or time.monotonic() > self._pending_until:
                self._pending_value = None
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        """Drop a queued write when the entity goes away."""
        if self._cancel_queued_write is not None:
            self._cancel_queued_write()
            self._cancel_queued_write = None
            _LOGGER.warning(f"Queued setpoint write for device {self._device_id} was dropped")
        await super().async_will_remove_from_hass()

    async def async_set_native_value(self, value: float) -> None:
        """Write a new setpoint, or queue it if the last write was too recent."""
        last_write = self.coordinator.last_write.get(str(self._device_id))
        wait = 0.0
        if last_write is not None:
            wait = last_write + WRITE_MIN_INTERVAL_SECONDS - time.monotonic()
        self._pending_value = value

        if wait > 0:
            # The pump drops a write that closely follows another. Send only the newest
            # value once the interval has passed.
            if self._cancel_queued_write is not None:
                self._cancel_queued_write()
            self._cancel_queued_write = async_call_later(self.hass, wait, self._async_send_queued)
            _LOGGER.info(
                f"Setpoint {value:.1f} for device {self._device_id} queued for {wait:.0f} s "
                f"to space it from the previous write"
            )
            self.async_write_ha_state()
            return

        await self._async_send(value)

    async def _async_send_queued(self, _now: datetime) -> None:
        """Send the newest queued value."""
        self._cancel_queued_write = None
        if self._pending_value is None:
            return
        try:
            await self._async_send(self._pending_value)
        except HomeAssistantError as err:
            # Nobody is awaiting a queued write, so log instead of raising
            _LOGGER.error(str(err))

    async def _async_send(self, value: float) -> None:
        """Write the value to the heat pump."""
        # Stamped before the call: a failed or timed-out request may still have reached the pump
        self.coordinator.last_write[str(self._device_id)] = time.monotonic()
        try:
            await self.hass.async_add_executor_job(
                self._api.set_property,
                self._device_id,
                PROPERTY_SET_INDOOR_TEMP,
                f"{value:.1f}",
            )
        except LoggameraAPIError as err:
            self._pending_value = None
            self.async_write_ha_state()
            raise HomeAssistantError(f"Could not set indoor temperature: {err}") from err

        _LOGGER.info(f"Set indoor temperature of device {self._device_id} to {value:.1f}")
        self._pending_until = time.monotonic() + PENDING_TIMEOUT_SECONDS
        self.async_write_ha_state()

        async def _refresh(_now: datetime) -> None:
            await self.coordinator.async_request_refresh()

        self.async_on_remove(async_call_later(self.hass, REFRESH_DELAY_SECONDS, _refresh))
