"""Helpers for Tab5 device entities."""

from __future__ import annotations

from typing import Any, Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    CONF_BASE_TOPIC,
    CONF_DEVICE_ID,
    DEFAULT_BASE,
    DOMAIN,
)


def _merged_entry_data(entry: ConfigEntry) -> Dict[str, Any]:
    data = dict(entry.data or {})
    if entry.options:
        data.update(entry.options)
    return data


def normalise_topic(value: str | None, default: str) -> str:
    result = (value or "").strip() or default
    while result.endswith("/"):
        result = result[:-1]
    return result or default


def entry_base_topic(entry: ConfigEntry) -> str:
    data = _merged_entry_data(entry)
    return normalise_topic(data.get(CONF_BASE_TOPIC), DEFAULT_BASE)


def entry_device_id(entry: ConfigEntry) -> str:
    data = _merged_entry_data(entry)
    return data.get(CONF_DEVICE_ID) or entry.entry_id


def entry_device_name(entry: ConfigEntry) -> str:
    data = _merged_entry_data(entry)
    device_id = data.get(CONF_DEVICE_ID)
    if device_id:
        suffix = str(device_id)[-4:].upper()
        return f"Tab5 {suffix}"
    return "Tab5 LVGL"


def entry_device_info(entry: ConfigEntry) -> DeviceInfo:
    device_id = entry_device_id(entry)
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=entry_device_name(entry),
        manufacturer="M5Stack",
        model="Tab5",
    )


def command_topic(base_topic: str, leaf: str) -> str:
    return f"{base_topic}/cmnd/{leaf}"


def state_topic(base_topic: str, leaf: str) -> str:
    return f"{base_topic}/stat/{leaf}"
