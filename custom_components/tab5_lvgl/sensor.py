"""Sensor entities for Tab5 runtime telemetry."""

from __future__ import annotations

import math

from homeassistant.components import mqtt
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HA_PREFIX, DEFAULT_PREFIX, TOPIC_SENSOR_SOC
from .device_helpers import (
    entry_base_topic,
    entry_device_id,
    entry_device_info,
    normalise_topic,
    sensor_topic,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    base_topic = entry_base_topic(entry)
    merged = dict(entry.data or {})
    if entry.options:
        merged.update(entry.options)
    ha_prefix = normalise_topic(merged.get(CONF_HA_PREFIX), DEFAULT_PREFIX)
    async_add_entities(
        [Tab5BatterySensor(entry, base_topic), Tab5ExternalTemperatureSensor(entry, ha_prefix)]
    )


class Tab5BatterySensor(SensorEntity):
    """Battery state-of-charge in percent."""

    _attr_name = "Batterie SoC (M5.Power)"
    _attr_object_id = "tab5_internal_battery_soc"
    _attr_icon = "mdi:battery"
    _attr_native_unit_of_measurement = "%"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_battery_soc"
        self._topic_state = sensor_topic(base_topic, TOPIC_SENSOR_SOC)
        self._unsub_state = None

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload.strip()
            if not raw:
                return
            if raw.endswith("%"):
                raw = raw[:-1].strip()
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return
            if math.isnan(value) or math.isinf(value):
                return
            value_int = int(round(value))
            if value_int < 0:
                value_int = 0
            if value_int > 100:
                value_int = 100
            self._attr_native_value = value_int
            self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()


class Tab5ExternalTemperatureSensor(SensorEntity):
    """External DS18x20 temperature from Tab5."""

    _attr_name = "DS18x20 Temperatur (GPIO 1/50)"
    _attr_object_id = "tab5_external_temperature"
    _attr_icon = "mdi:thermometer"
    _attr_native_unit_of_measurement = "C"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, entry: ConfigEntry, ha_prefix: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_external_temperature"
        self._topic_state = f"{ha_prefix}/sensor/tab5_external_temperature/state"
        self._unsub_state = None
        self._attr_available = False

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload.strip()
            if not raw:
                return
            lowered = raw.lower()
            if lowered in {"unknown", "unavailable", "nan", "inf", "-inf"}:
                self._attr_available = False
                self._attr_native_value = None
                self.async_write_ha_state()
                return
            raw = raw.replace(",", ".")
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return
            if math.isnan(value) or math.isinf(value):
                self._attr_available = False
                self._attr_native_value = None
            else:
                self._attr_available = True
                self._attr_native_value = round(value, 1)
            self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()
