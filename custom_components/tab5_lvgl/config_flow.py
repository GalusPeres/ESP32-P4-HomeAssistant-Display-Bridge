# Config flow + options flow for the Tab5 LVGL integration.

from __future__ import annotations

import logging
from typing import Any, Dict

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
  CONF_BASE_TOPIC,
  CONF_DEVICE_ID,
  CONF_DEVICE_NAME,
  CONF_HA_PREFIX,
  CONF_LIGHTS,
  CONF_MANUFACTURER,
  CONF_MODEL,
  CONF_SCENE_ENTITIES,
  CONF_SCENE_MAP,
  CONF_SCENE_MAP_TEXT,
  CONF_SENSORS,
  CONF_SWITCHES,
  DEFAULT_BASE,
  DEFAULT_PREFIX,
  DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Config flow — adding a new panel (device info only)
# ---------------------------------------------------------------------------

class Tab5ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
  """Handle adding a new panel."""

  VERSION = 1

  async def async_step_user(self, user_input: Dict[str, Any] | None = None):
    errors: Dict[str, str] = {}

    if user_input is not None:
      base = _normalise_topic(user_input.get(CONF_BASE_TOPIC, DEFAULT_BASE), DEFAULT_BASE)
      prefix = _normalise_topic(user_input.get(CONF_HA_PREFIX, DEFAULT_PREFIX), DEFAULT_PREFIX)

      for entry in self._async_current_entries():
        if entry.data.get(CONF_BASE_TOPIC) == base:
          errors["base_topic"] = "topic_already_configured"
          break

      if not errors:
        data: Dict[str, Any] = {CONF_BASE_TOPIC: base, CONF_HA_PREFIX: prefix}
        for key in (CONF_DEVICE_NAME, CONF_MANUFACTURER, CONF_MODEL):
          val = (user_input.get(key) or "").strip()
          if val:
            data[key] = val
        title = _entry_title(data)
        return self.async_create_entry(title=title, data=data)

    defaults = user_input or {}
    return self.async_show_form(
      step_id="user",
      data_schema=_build_device_schema(defaults),
      errors=errors,
    )

  async def async_step_import(self, import_data: Dict[str, Any]):
    device_id = import_data.get(CONF_DEVICE_ID)
    if device_id:
      await self.async_set_unique_id(device_id)
      self._abort_if_unique_id_configured()
    title = _entry_title(import_data)
    return self.async_create_entry(title=title, data=import_data)

  @staticmethod
  @callback
  def async_get_options_flow(
    config_entry: config_entries.ConfigEntry,
  ) -> config_entries.OptionsFlow:
    return Tab5OptionsFlowHandler()


# ---------------------------------------------------------------------------
#  Options flow — device info + shared entity configuration
# ---------------------------------------------------------------------------

class Tab5OptionsFlowHandler(config_entries.OptionsFlow):
  """Edit panel settings and shared entity configuration."""

  async def async_step_init(self, user_input: Dict[str, Any] | None = None):
    return await self.async_step_user(user_input)

  async def async_step_user(self, user_input: Dict[str, Any] | None = None):
    errors: Dict[str, str] = {}
    current = dict(self.config_entry.data)

    if user_input is not None:
      try:
        data = _convert_options_data(user_input, current)
      except ValueError as err:
        errors["base"] = err.args[0]
      else:
        _LOGGER.warning(
          "Tab5 LVGL speichert: sensors=%s lights=%s switches=%s scene_map=%s",
          data.get(CONF_SENSORS),
          data.get(CONF_LIGHTS),
          data.get(CONF_SWITCHES),
          data.get(CONF_SCENE_MAP),
        )
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        return self.async_create_entry(title="", data={})

    defaults = _entry_to_form_data(current)
    return self.async_show_form(
      step_id="user",
      data_schema=_build_full_schema(defaults),
      errors=errors,
    )


# ---------------------------------------------------------------------------
#  Schemas
# ---------------------------------------------------------------------------

def _build_device_schema(defaults: Dict[str, Any]) -> vol.Schema:
  """Schema for adding a new panel — device info only."""
  return vol.Schema(
    {
      vol.Required(CONF_BASE_TOPIC, default=defaults.get(CONF_BASE_TOPIC, DEFAULT_BASE)): str,
      vol.Required(CONF_HA_PREFIX, default=defaults.get(CONF_HA_PREFIX, DEFAULT_PREFIX)): str,
      vol.Optional(CONF_DEVICE_NAME, default=defaults.get(CONF_DEVICE_NAME, "")): str,
      vol.Optional(CONF_MANUFACTURER, default=defaults.get(CONF_MANUFACTURER, "")): str,
      vol.Optional(CONF_MODEL, default=defaults.get(CONF_MODEL, "")): str,
    }
  )


def _build_full_schema(defaults: Dict[str, Any]) -> vol.Schema:
  """Schema for options — device info + shared entity configuration."""
  return vol.Schema(
    {
      vol.Required(CONF_BASE_TOPIC, default=defaults.get(CONF_BASE_TOPIC, DEFAULT_BASE)): str,
      vol.Required(CONF_HA_PREFIX, default=defaults.get(CONF_HA_PREFIX, DEFAULT_PREFIX)): str,
      vol.Optional(CONF_DEVICE_NAME, default=defaults.get(CONF_DEVICE_NAME, "")): str,
      vol.Optional(CONF_MANUFACTURER, default=defaults.get(CONF_MANUFACTURER, "")): str,
      vol.Optional(CONF_MODEL, default=defaults.get(CONF_MODEL, "")): str,
      vol.Optional(CONF_SENSORS, default=defaults.get(CONF_SENSORS, [])): selector.EntitySelector(
        selector.EntitySelectorConfig(multiple=True)
      ),
      vol.Optional(CONF_LIGHTS, default=defaults.get(CONF_LIGHTS, [])): selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["light"], multiple=True)
      ),
      vol.Optional(CONF_SWITCHES, default=defaults.get(CONF_SWITCHES, [])): selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["switch"], multiple=True)
      ),
      vol.Optional(CONF_SCENE_ENTITIES, default=defaults.get(CONF_SCENE_ENTITIES, [])): selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["scene", "script"], multiple=True)
      ),
      vol.Optional(CONF_SCENE_MAP_TEXT, default=defaults.get(CONF_SCENE_MAP_TEXT, "")): selector.TextSelector(
        selector.TextSelectorConfig(multiline=True)
      ),
    }
  )


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _entry_to_form_data(source: Dict[str, Any]) -> Dict[str, Any]:
  data = dict(source)
  data.setdefault(CONF_BASE_TOPIC, DEFAULT_BASE)
  data.setdefault(CONF_HA_PREFIX, DEFAULT_PREFIX)
  data.setdefault(CONF_DEVICE_NAME, "")
  data.setdefault(CONF_MANUFACTURER, "")
  data.setdefault(CONF_MODEL, "")
  data.setdefault(CONF_SENSORS, [])
  data.setdefault(CONF_LIGHTS, [])
  data.setdefault(CONF_SWITCHES, [])
  data.setdefault(CONF_SCENE_ENTITIES, list((source.get(CONF_SCENE_MAP) or {}).values()))
  data.setdefault(CONF_SCENE_MAP_TEXT, source.get(CONF_SCENE_MAP_TEXT, ""))
  return data


def _convert_options_data(user_input: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
  _LOGGER.debug("Tab5 options raw form data: %s", user_input)

  base = _normalise_topic(
    user_input.get(CONF_BASE_TOPIC, current.get(CONF_BASE_TOPIC, DEFAULT_BASE)),
    DEFAULT_BASE,
  )
  prefix = _normalise_topic(
    user_input.get(CONF_HA_PREFIX, current.get(CONF_HA_PREFIX, DEFAULT_PREFIX)),
    DEFAULT_PREFIX,
  )

  raw_sensors = user_input.get(CONF_SENSORS)
  if raw_sensors is None:
    raw_sensors = current.get(CONF_SENSORS, [])
  sensors = _normalise_entity_list(raw_sensors)

  raw_lights = user_input.get(CONF_LIGHTS)
  if raw_lights is None:
    raw_lights = current.get(CONF_LIGHTS, [])
  lights = _normalise_entity_list(raw_lights)

  raw_switches = user_input.get(CONF_SWITCHES)
  if raw_switches is None:
    raw_switches = current.get(CONF_SWITCHES, [])
  switches = _normalise_entity_list(raw_switches)

  scene_map = {}

  selected_scenes = user_input.get(CONF_SCENE_ENTITIES)
  if selected_scenes is None:
    selected_scenes = list((current.get(CONF_SCENE_MAP) or {}).values())
  else:
    selected_scenes = _normalise_entity_list(selected_scenes)

  for entity_id in selected_scenes:
    entity_id = (entity_id or "").strip()
    if not entity_id:
      continue
    alias = entity_id.split(".", 1)[-1]
    alias = alias.replace("scene.", "").lower()
    base_alias = alias
    idx = 2
    while alias in scene_map:
      alias = f"{base_alias}{idx}"
      idx += 1
    scene_map[alias] = entity_id

  scene_map_text = user_input.get(CONF_SCENE_MAP_TEXT, current.get(CONF_SCENE_MAP_TEXT, ""))
  scene_map_text = scene_map_text.strip("\n")
  manual_map = _parse_scene_map(scene_map_text)
  scene_map.update(manual_map)

  device_name = (user_input.get(CONF_DEVICE_NAME) or current.get(CONF_DEVICE_NAME, "")).strip()
  manufacturer = (user_input.get(CONF_MANUFACTURER) or current.get(CONF_MANUFACTURER, "")).strip()
  model = (user_input.get(CONF_MODEL) or current.get(CONF_MODEL, "")).strip()

  updated = dict(current)
  updated[CONF_BASE_TOPIC] = base
  updated[CONF_HA_PREFIX] = prefix
  updated[CONF_SENSORS] = sensors
  updated[CONF_LIGHTS] = lights
  updated[CONF_SWITCHES] = switches
  updated[CONF_SCENE_MAP] = scene_map
  updated[CONF_SCENE_MAP_TEXT] = scene_map_text
  for key, val in ((CONF_DEVICE_NAME, device_name), (CONF_MANUFACTURER, manufacturer), (CONF_MODEL, model)):
    if val:
      updated[key] = val
    else:
      updated.pop(key, None)
  return updated


def _parse_scene_map(text: str) -> Dict[str, str]:
  mapping: Dict[str, str] = {}
  for raw_line in text.splitlines():
    line = raw_line.strip()
    if not line:
      continue
    if "=" not in line:
      raise ValueError("invalid_scene_map")
    alias, entity = line.split("=", 1)
    alias = alias.strip().lower()
    entity = entity.strip()
    if not alias or not entity:
      raise ValueError("invalid_scene_map")
    mapping[alias] = entity
  return mapping


def _normalise_topic(value: str | None, default: str) -> str:
  result = (value or "").strip() or default
  while result.endswith("/"):
    result = result[:-1]
  return result or default


def _entry_title(data: Dict[str, Any]) -> str:
  device_name = data.get(CONF_DEVICE_NAME)
  if device_name:
    return device_name
  device_id = data.get(CONF_DEVICE_ID)
  if device_id:
    suffix = device_id[-4:].upper()
    return f"Tab5 {suffix}"
  return "LVGL Panel"


def _normalise_entity_list(values: Any) -> list[str]:
  entities = []
  if not isinstance(values, list):
    values = [values] if values is not None else []
  for item in values:
    if isinstance(item, str):
      entity_id = item.strip()
    elif isinstance(item, dict):
      entity_id = str(item.get("entity_id", "")).strip()
    else:
      entity_id = str(item or "").strip()
    if entity_id:
      entities.append(entity_id)
  return entities
