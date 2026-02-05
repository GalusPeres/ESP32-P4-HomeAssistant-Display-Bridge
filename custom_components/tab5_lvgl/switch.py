"""Switch entities for Tab5 device settings."""

from __future__ import annotations

from homeassistant.components import mqtt
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from .const import TOPIC_DISPLAY_ROTATE, TOPIC_SLEEP_BATTERY, TOPIC_SLEEP_MAINS
from .device_helpers import (
    command_topic,
    entry_base_topic,
    entry_device_id,
    entry_device_info,
    state_topic,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    base_topic = entry_base_topic(entry)
    async_add_entities(
        [
            Tab5RotateSwitch(entry, base_topic),
            Tab5AutoSleepSwitch(entry, base_topic),
        ]
    )


class Tab5RotateSwitch(SwitchEntity):
    """Switch to rotate the display 180 degrees."""

    _attr_name = "Display Rotation"
    _attr_icon = "mdi:phone-rotate-portrait"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_display_rotate"
        self._topic_cmd = command_topic(base_topic, TOPIC_DISPLAY_ROTATE)
        self._topic_state = state_topic(base_topic, TOPIC_DISPLAY_ROTATE)
        self._unsub_state = None

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_state(msg: mqtt.ReceiveMessage) -> None:
            raw = msg.payload.strip().lower()
            if raw in {"on", "1", "true", "yes"}:
                self._attr_is_on = True
            elif raw in {"off", "0", "false", "no"}:
                self._attr_is_on = False
            else:
                return
            self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs) -> None:
        await mqtt.async_publish(self.hass, self._topic_cmd, "ON", qos=0, retain=False)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await mqtt.async_publish(self.hass, self._topic_cmd, "OFF", qos=0, retain=False)
        self._attr_is_on = False
        self.async_write_ha_state()


def _sleep_enabled_from_label(label: str) -> bool:
    text = (label or "").strip().lower()
    if not text:
        return False
    return text not in {"nie", "never", "off", "0"}


class Tab5AutoSleepSwitch(SwitchEntity):
    """Master switch to enable/disable auto-sleep."""

    _attr_name = "Auto-Sleep"
    _attr_icon = "mdi:sleep"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = f"{entry_device_id(entry)}_auto_sleep"
        self._topic_mains_cmd = command_topic(base_topic, TOPIC_SLEEP_MAINS)
        self._topic_bat_cmd = command_topic(base_topic, TOPIC_SLEEP_BATTERY)
        self._topic_mains_state = state_topic(base_topic, TOPIC_SLEEP_MAINS)
        self._topic_bat_state = state_topic(base_topic, TOPIC_SLEEP_BATTERY)
        self._unsub_mains = None
        self._unsub_bat = None
        self._mains_enabled = None
        self._bat_enabled = None
        self._last_mains = None
        self._last_bat = None

    @property
    def device_info(self):
        return self._device_info

    def _update_state(self) -> None:
        mains = bool(self._mains_enabled) if self._mains_enabled is not None else False
        bat = bool(self._bat_enabled) if self._bat_enabled is not None else False
        self._attr_is_on = mains or bat
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _handle_mains(msg: mqtt.ReceiveMessage) -> None:
            label = msg.payload.strip()
            enabled = _sleep_enabled_from_label(label)
            self._mains_enabled = enabled
            if enabled and label:
                self._last_mains = label
            self._update_state()

        async def _handle_bat(msg: mqtt.ReceiveMessage) -> None:
            label = msg.payload.strip()
            enabled = _sleep_enabled_from_label(label)
            self._bat_enabled = enabled
            if enabled and label:
                self._last_bat = label
            self._update_state()

        self._unsub_mains = await mqtt.async_subscribe(
            self.hass, self._topic_mains_state, _handle_mains
        )
        self._unsub_bat = await mqtt.async_subscribe(
            self.hass, self._topic_bat_state, _handle_bat
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_mains:
            self._unsub_mains()
            self._unsub_mains = None
        if self._unsub_bat:
            self._unsub_bat()
            self._unsub_bat = None
        await super().async_will_remove_from_hass()

    async def async_turn_on(self, **kwargs) -> None:
        mains = self._last_mains or "60 s"
        bat = self._last_bat or mains
        await mqtt.async_publish(self.hass, self._topic_mains_cmd, mains, qos=0, retain=False)
        await mqtt.async_publish(self.hass, self._topic_bat_cmd, bat, qos=0, retain=False)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await mqtt.async_publish(self.hass, self._topic_mains_cmd, "Nie", qos=0, retain=False)
        await mqtt.async_publish(self.hass, self._topic_bat_cmd, "Nie", qos=0, retain=False)
        self._attr_is_on = False
        self.async_write_ha_state()
