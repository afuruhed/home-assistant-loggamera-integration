"""Support for Loggamera heat pump setpoint (number) entities."""

import logging
from datetime import datetime
from typing import Optional

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import LoggameraAPIError
from .const import DOMAIN, PROPERTY_SET_INDOOR_TEMP

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

    entities = [
        LoggameraIndoorSetpoint(coordinator, api, device)
        for device in coordinator.data.get("devices", [])
        if device.get("Class") == "HeatPump"
        and PROPERTY_SET_INDOOR_TEMP in coordinator.write_capabilities.get(str(device["Id"]), [])
    ]
    if entities:
        async_add_entities(entities)


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
        # Value just written, shown until the next refresh reports the device's own value
        self._pending_value: Optional[float] = None

    @property
    def device_info(self) -> DeviceInfo:
        """Attach to the same device as the heat pump's sensors."""
        return DeviceInfo(
            identifiers={(DOMAIN, str(self._device_id))},
            name=f"{self._device_type} - {self._device_name}",
            manufacturer="Loggamera",
            model=f"Loggamera {self._device_type}",
        )

    @property
    def native_value(self) -> Optional[float]:
        """Return the current setpoint."""
        if self._pending_value is not None:
            return self._pending_value
        settings = self.coordinator.data.get("device_data", {}).get(
            f"settings_{self._device_id}", {}
        )
        value = settings.get(PROPERTY_SET_INDOOR_TEMP)
        return float(value) if value is not None else None

    @property
    def available(self) -> bool:
        """Available while the heat pump settings were read successfully."""
        return self.native_value is not None

    def _handle_coordinator_update(self) -> None:
        """Drop the optimistic value once the device reports data again."""
        self._pending_value = None
        super()._handle_coordinator_update()

    async def async_set_native_value(self, value: float) -> None:
        """Write a new setpoint to the heat pump."""
        try:
            await self.hass.async_add_executor_job(
                self._api.set_property,
                self._device_id,
                PROPERTY_SET_INDOOR_TEMP,
                f"{value:.1f}",
            )
        except LoggameraAPIError as err:
            raise HomeAssistantError(f"Could not set indoor temperature: {err}") from err

        _LOGGER.info(f"Set indoor temperature of device {self._device_id} to {value:.1f}")
        self._pending_value = value
        self.async_write_ha_state()

        async def _refresh(_now: datetime) -> None:
            await self.coordinator.async_request_refresh()

        self.async_on_remove(async_call_later(self.hass, REFRESH_DELAY_SECONDS, _refresh))
