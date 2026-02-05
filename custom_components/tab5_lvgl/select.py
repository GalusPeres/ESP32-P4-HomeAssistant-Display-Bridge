"""Select entities for Tab5 device settings."""

from __future__ import annotations

from homeassistant.components import mqtt
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from .const import SLEEP_OPTIONS, TOPIC_SLEEP_BATTERY, TOPIC_SLEEP_MAINS
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
            Tab5SleepSelect(
                entry,
                base_topic,
                TOPIC_SLEEP_MAINS,
                f"{entry_device_id(entry)}_sleep_mains",
                "Auto-Sleep Netzteil",
                "mdi:power-plug",
            ),
            Tab5SleepSelect(
                entry,
                base_topic,
                TOPIC_SLEEP_BATTERY,
                f"{entry_device_id(entry)}_sleep_battery",
                "Auto-Sleep Batterie",
                "mdi:battery",
            ),
        ]
    )


class Tab5SleepSelect(SelectEntity):
    """Auto-sleep select."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = SLEEP_OPTIONS

    def __init__(
        self,
        entry: ConfigEntry,
        base_topic: str,
        leaf: str,
        unique_id: str,
        name: str,
        icon: str,
    ) -> None:
        self._entry = entry
        self._device_info = entry_device_info(entry)
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._attr_icon = icon
        self._topic_cmd = command_topic(base_topic, leaf)
        self._topic_state = state_topic(base_topic, leaf)
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
            if raw not in SLEEP_OPTIONS:
                raw = raw.strip().title()
            if raw not in SLEEP_OPTIONS:
                if raw.lower() in {"off", "never", "nie"}:
                    raw = "Nie"
            if raw in SLEEP_OPTIONS:
                self._attr_current_option = raw
                self.async_write_ha_state()

        self._unsub_state = await mqtt.async_subscribe(
            self.hass, self._topic_state, _handle_state
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        await super().async_will_remove_from_hass()

    async def async_select_option(self, option: str) -> None:
        if option not in SLEEP_OPTIONS:
            return
        await mqtt.async_publish(self.hass, self._topic_cmd, option, qos=0, retain=False)
        self._attr_current_option = option
        self.async_write_ha_state()
