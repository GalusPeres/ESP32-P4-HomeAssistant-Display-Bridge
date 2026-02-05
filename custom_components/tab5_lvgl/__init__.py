"""Home Assistant integration for the Tab5 LVGL dashboard."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import mqtt
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.components.recorder import get_instance
try:
  from homeassistant.components.recorder.history import get_significant_states
except ImportError:  # pragma: no cover - older HA fallback
  get_significant_states = None
try:
  from homeassistant.components.recorder.history import state_changes_during_period
except ImportError:  # pragma: no cover - older HA fallback
  state_changes_during_period = None
try:
  from homeassistant.components.weather import async_get_forecasts
except Exception:  # pragma: no cover - optional weather helper
  async_get_forecasts = None
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
try:
  from homeassistant.helpers.icon import icon_for_entity
except Exception:  # pragma: no cover - optional fallback
  icon_for_entity = None
from homeassistant.util import dt as dt_util

from .const import (
  CONF_BASE_TOPIC,
  CONF_DEVICE_ID,
  CONF_HA_PREFIX,
  CONF_LIGHTS,
  CONF_SCENE_MAP,
  CONF_SENSORS,
  CONF_SWITCHES,
  CONF_WEATHERS,
  CONFIG_TOPIC_ROOT,
  CONFIG_TOPIC_SUB,
  DEFAULT_BASE,
  DEFAULT_PREFIX,
  DOMAIN,
  HISTORY_REQUEST_SUFFIX,
  HISTORY_RESPONSE_SUFFIX,
  SERVICE_PUBLISH_SNAPSHOT,
)
from .device_helpers import entry_device_id, entry_device_name

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["number", "select", "switch"]

LIGHT_SERVICE_FIELDS = {
  "transition",
  "brightness",
  "brightness_pct",
  "rgb_color",
  "rgbw_color",
  "rgbww_color",
  "color_temp",
  "color_temp_kelvin",
  "color_name",
  "hs_color",
  "xy_color",
  "effect",
  "flash",
  "white",
  "kelvin",
}

SERVICE_SCHEMA = vol.Schema({vol.Optional("entry_id"): cv.string})

FORECAST_TYPE = "daily"
FORECAST_LIMIT = 5
FORECAST_CACHE_TTL = timedelta(minutes=10)


async def async_setup(hass: HomeAssistant, config: Dict[str, Any]) -> bool:
  """Set up the integration namespace and service."""
  domain_data = hass.data.setdefault(DOMAIN, {"entries": {}})

  if not hass.services.has_service(DOMAIN, SERVICE_PUBLISH_SNAPSHOT):
    async def handle_service(call: ServiceCall) -> None:
      bridge = _resolve_bridge(hass, call.data.get("entry_id"))
      if bridge is None:
          raise HomeAssistantError("No Tab5 LVGL entry configured")
      await bridge.async_publish_snapshot()

    hass.services.async_register(
      DOMAIN,
      SERVICE_PUBLISH_SNAPSHOT,
      handle_service,
      schema=SERVICE_SCHEMA,
    )

  async def _handle_bridge_config(msg: ReceiveMessage) -> None:
    try:
      payload = json.loads(msg.payload)
    except (ValueError, TypeError):
      _LOGGER.warning("Tab5 LVGL: Ungültige Bridge-Konfiguration erhalten: %s", msg.payload)
      return
    await _async_process_bridge_config(hass, payload)

  if "_config_unsub" not in domain_data:
    domain_data["_config_unsub"] = await mqtt.async_subscribe(
      hass,
      CONFIG_TOPIC_SUB,
      _handle_bridge_config,
    )

  return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
  """Create the bridge instance for a config entry."""
  device_reg = dr.async_get(hass)
  device_reg.async_get_or_create(
    config_entry_id=entry.entry_id,
    identifiers={(DOMAIN, entry_device_id(entry))},
    name=entry_device_name(entry),
    manufacturer="M5Stack",
    model="Tab5",
  )
  bridge = Tab5Bridge(hass, entry)
  await bridge.async_setup()
  hass.data[DOMAIN]["entries"][entry.entry_id] = bridge
  entry.async_on_unload(entry.add_update_listener(_async_update_listener))
  await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
  await bridge.async_publish_config_to_device()
  return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
  """Unload a config entry."""
  bridge: Tab5Bridge | None = hass.data[DOMAIN]["entries"].get(entry.entry_id)
  unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
  if unload_ok and bridge is not None:
    await bridge.async_unload()
    hass.data[DOMAIN]["entries"].pop(entry.entry_id, None)
  return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
  """Reload when the config entry is updated."""
  await hass.config_entries.async_reload(entry.entry_id)


def _resolve_bridge(hass: HomeAssistant, entry_id: Optional[str]) -> Optional["Tab5Bridge"]:
  entries = hass.data.get(DOMAIN, {}).get("entries", {})
  if not entries:
    return None

  if entry_id:
    return entries.get(entry_id)

  # Fallback to the first (and typically only) entry
  return next(iter(entries.values()))


class Tab5Bridge:
  """Copies Home Assistant state to the Tab5 MQTT topics."""

  def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
    self.hass = hass
    self.entry = entry

    data = dict(entry.data or {})
    if entry.options:
      # Options override stored data (HA keeps UI edits in entry.options).
      data.update(entry.options)
    self.device_id = data.get(CONF_DEVICE_ID)
    self.base_topic = _normalise_topic(data.get(CONF_BASE_TOPIC, DEFAULT_BASE), DEFAULT_BASE)
    self.ha_prefix = _normalise_topic(data.get(CONF_HA_PREFIX, DEFAULT_PREFIX), DEFAULT_PREFIX)
    raw_sensors = _unique_entities(list(data.get(CONF_SENSORS, [])))
    raw_weathers = _unique_entities(list(data.get(CONF_WEATHERS, [])))
    if raw_weathers:
      raw_sensors = _unique_entities(raw_sensors + raw_weathers)
    self.weathers, self.sensors = _split_weather_entities(raw_sensors)
    self.lights: List[str] = _unique_entities(list(data.get(CONF_LIGHTS, [])))
    self.switches: List[str] = _unique_entities(list(data.get(CONF_SWITCHES, [])))
    self.tracked_entities: List[str] = _unique_entities(self.sensors + self.lights + self.switches + self.weathers)
    self.scene_map: Dict[str, str] = {
      (alias or "").lower(): entity
      for alias, entity in (data.get(CONF_SCENE_MAP, {}) or {}).items()
    }
    self.config_topic = f"{CONFIG_TOPIC_ROOT}/{self.device_id}/bridge/apply" if self.device_id else None
    self.history_request_topic = (
      f"{CONFIG_TOPIC_ROOT}/{self.device_id}/{HISTORY_REQUEST_SUFFIX}" if self.device_id else None
    )
    self.history_response_topic = (
      f"{CONFIG_TOPIC_ROOT}/{self.device_id}/{HISTORY_RESPONSE_SUFFIX}" if self.device_id else None
    )
    self._unsub_state = None
    self._unsub_connected = None
    self._unsub_scene = None
    self._unsub_light = None
    self._unsub_switch = None
    self._unsub_request = None
    self._unsub_history = None
    self._config_refresh_handles: List = []
    self._config_refresh_pending = 0
    self._icon_cache: Dict[str, str] = {}
    self._icon_refresh_handle = None
    self._forecast_cache: Dict[str, Tuple[datetime, List[Dict[str, Any]]]] = {}

  async def async_setup(self) -> None:
    """Subscribe to MQTT topics and start observers."""
    self._unsub_connected = await mqtt.async_subscribe(
      self.hass,
      f"{self.base_topic}/stat/connected",
      self._async_handle_connected,
    )
    self._unsub_scene = await mqtt.async_subscribe(
      self.hass,
      f"{self.base_topic}/cmnd/scene",
      self._async_handle_scene_command,
    )

    if self.lights:
      self._unsub_light = await mqtt.async_subscribe(
        self.hass,
        f"{self.base_topic}/cmnd/light",
        self._async_handle_light_command,
      )

    if self.switches:
      self._unsub_switch = await mqtt.async_subscribe(
        self.hass,
        f"{self.base_topic}/cmnd/switch",
        self._async_handle_switch_command,
      )

    if self.tracked_entities:
      self._unsub_state = async_track_state_change_event(
        self.hass,
        self.tracked_entities,
        self._handle_state_event,
      )

    self._prime_icon_cache()

    _LOGGER.info(
      "Tab5 MQTT bridge ready (device=%s, base=%s, ha_prefix=%s, sensors=%d, lights=%d, switches=%d)",
      self.device_id or "n/a",
      self.base_topic,
      self.ha_prefix,
      len(self.sensors),
      len(self.lights),
      len(self.switches),
    )
    if self.config_topic:
      request_topic = f"{CONFIG_TOPIC_ROOT}/{self.device_id}/bridge/request"
      self._unsub_request = await mqtt.async_subscribe(
        self.hass,
        request_topic,
        self._async_handle_request,
      )
      _LOGGER.debug("Tab5 subscribed to request topic %s", request_topic)
    if self.history_request_topic:
      self._unsub_history = await mqtt.async_subscribe(
        self.hass,
        self.history_request_topic,
        self._async_handle_history_request,
      )
      _LOGGER.debug("Tab5 subscribed to history topic %s", self.history_request_topic)
    self._schedule_config_refresh()

  async def async_unload(self) -> None:
    """Cleanup subscriptions."""
    if self._unsub_state:
      self._unsub_state()
      self._unsub_state = None
    if self._unsub_connected:
      self._unsub_connected()
      self._unsub_connected = None
    if self._unsub_scene:
      self._unsub_scene()
      self._unsub_scene = None
    if self._unsub_light:
      self._unsub_light()
      self._unsub_light = None
    if self._unsub_switch:
      self._unsub_switch()
      self._unsub_switch = None
    if hasattr(self, "_unsub_request") and self._unsub_request:
      self._unsub_request()
      self._unsub_request = None
    if self._unsub_history:
      self._unsub_history()
      self._unsub_history = None
    if self._config_refresh_handles:
      for unsub in self._config_refresh_handles:
        unsub()
      self._config_refresh_handles = []
      self._config_refresh_pending = 0
    if self._icon_refresh_handle:
      self._icon_refresh_handle()
      self._icon_refresh_handle = None

  async def async_publish_config_to_device(self) -> None:
    if not self.config_topic or not self.device_id:
      return
    payload = json.dumps(
      {
        "device_id": self.device_id,
        "base_topic": self.base_topic,
        "ha_prefix": self.ha_prefix,
        "sensors": self.sensors,
        "sensor_meta": self._build_sensor_meta(),
        CONF_WEATHERS: self.weathers,
        "weather_meta": self._build_weather_meta(),
        "lights": self.lights,
        "light_meta": self._build_entity_meta(self.lights),
        "switches": self.switches,
        "switch_meta": self._build_entity_meta(self.switches),
        "scene_meta": self._build_scene_meta(),
        "scene_map": self.scene_map,
      }
    )
    _LOGGER.warning(
      "Tab5 LVGL DEBUG: Publishing config to topic '%s':\n%s",
      self.config_topic,
      payload
    )
    await mqtt.async_publish(
      self.hass,
      self.config_topic,
      payload,
      qos=1,
      retain=True,
    )

  async def async_publish_snapshot(self) -> None:
    """Push all configured entities to MQTT."""
    for entity_id in self.tracked_entities:
      state = self.hass.states.get(entity_id)
      if not state:
        continue
      topic = self._ha_topic_for_entity(entity_id, "state")
      payload = self._build_state_payload(entity_id, state)
      await mqtt.async_publish(self.hass, topic, payload, qos=0, retain=True)
      if _is_weather_entity(entity_id):
        await self._async_publish_weather_state(entity_id, state, retain=True)


  async def _async_handle_connected(self, msg: ReceiveMessage) -> None:
    """Handle Tab5 connection event."""
    if msg.payload == "1":
      _LOGGER.debug("Tab5 connected -> push config + snapshot")
      await self.async_publish_config_to_device()
      await self.async_publish_snapshot()
      self._schedule_config_refresh()

  async def _async_handle_request(self, msg: ReceiveMessage) -> None:
    """Handle explicit bridge refresh requests."""
    _LOGGER.debug("Tab5 requested bridge refresh via %s", msg.topic)
    await self.async_publish_config_to_device()

  async def _async_handle_history_request(self, msg: ReceiveMessage) -> None:
    """Handle history requests from the Tab5 popup."""
    if not self.history_response_topic:
      return

    parsed = _try_parse_json(msg.payload)
    if not isinstance(parsed, dict):
      _LOGGER.warning("Tab5 history request ignored (invalid payload): %s", msg.payload)
      return

    entity_id = str(parsed.get("entity_id") or "").strip()
    if not entity_id:
      _LOGGER.warning("Tab5 history request ignored (missing entity_id)")
      return

    hours = _coerce_int(parsed.get("hours"), 24, 1, 72)
    period_minutes = _coerce_int(parsed.get("period_minutes"), 5, 1, 60)
    points = _coerce_int(parsed.get("points"), int(hours * 60 / period_minutes), 1, 720)
    stat = str(parsed.get("stat") or "mean").strip().lower() or "mean"

    end = dt_util.utcnow()
    start = end - timedelta(hours=hours)

    def _coerce_float(raw: Any) -> Optional[float]:
      if raw is None:
        return None
      try:
        return float(raw)
      except (TypeError, ValueError):
        return None

    if stat not in {"mean", "min", "max", "last"}:
      stat = "mean"

    def _fetch_history_values() -> List[Optional[float]]:
      if points <= 0 or (state_changes_during_period is None and get_significant_states is None):
        return [None] * points

      history = None
      if state_changes_during_period is not None:
        try:
          history = state_changes_during_period(
            self.hass,
            start,
            end,
            entity_id,
            include_start_time_state=True,
            minimal_response=True,
            no_attributes=True,
          )
        except TypeError:
          history = state_changes_during_period(self.hass, start, end, entity_id)
      elif get_significant_states is not None:
        try:
          history = get_significant_states(
            self.hass,
            start,
            end,
            [entity_id],
            include_start_time_state=True,
            minimal_response=True,
            no_attributes=True,
          )
        except TypeError:
          history = get_significant_states(self.hass, start, end, [entity_id])

      states = history.get(entity_id, []) if history else []
      if not states:
        return [None] * points

      bucket_seconds = max(period_minutes, 1) * 60
      sums = [0.0] * points
      counts = [0] * points
      mins: List[Optional[float]] = [None] * points
      maxs: List[Optional[float]] = [None] * points
      lasts: List[Optional[float]] = [None] * points

      for state in states:
        state_time = getattr(state, "last_changed", None) or getattr(state, "last_updated", None)
        if state_time is None:
          continue
        idx = int((state_time - start).total_seconds() / bucket_seconds)
        if idx < 0:
          continue
        if idx >= points:
          idx = points - 1
        value = _coerce_float(getattr(state, "state", None))
        if value is None:
          continue
        counts[idx] += 1
        sums[idx] += value
        if mins[idx] is None or value < mins[idx]:
          mins[idx] = value
        if maxs[idx] is None or value > maxs[idx]:
          maxs[idx] = value
        lasts[idx] = value

      values: List[Optional[float]] = []
      prev_value: Optional[float] = None
      for idx in range(points):
        if counts[idx] > 0:
          if stat == "min":
            value = mins[idx]
          elif stat == "max":
            value = maxs[idx]
          elif stat == "last":
            value = lasts[idx]
          else:
            value = sums[idx] / counts[idx]
          prev_value = value
        else:
          value = prev_value
        values.append(round(value, 3) if value is not None else None)

      return values

    values = await get_instance(self.hass).async_add_executor_job(_fetch_history_values)

    state = self.hass.states.get(entity_id)
    response: Dict[str, Any] = {
      "entity_id": entity_id,
      "hours": hours,
      "period_minutes": period_minutes,
      "stat": stat,
      "values": values,
    }

    if state:
      unit = state.attributes.get("unit_of_measurement")
      if isinstance(unit, str) and unit.strip():
        response["unit"] = unit.strip()
      name = state.name
      if isinstance(name, str) and name.strip():
        response["name"] = name.strip()
      response["current"] = state.state

    await mqtt.async_publish(
      self.hass,
      self.history_response_topic,
      json.dumps(response, separators=(",", ":")),
      qos=0,
      retain=False,
    )

  async def _async_handle_scene_command(self, msg: ReceiveMessage) -> None:
    """Execute scene commands originating from the Tab5."""
    payload = msg.payload.strip()
    if not payload:
      return

    entity_id: Optional[str]
    if payload.startswith("scene."):
      entity_id = payload
    else:
      entity_id = self.scene_map.get(payload.lower())

    if not entity_id:
      _LOGGER.warning("Unhandled scene command from Tab5: %s", payload)
      return

    await self.hass.services.async_call(
      "scene",
      "turn_on",
      {"entity_id": entity_id},
      blocking=False,
    )

  async def _async_handle_light_command(self, msg: ReceiveMessage) -> None:
    """Execute light commands originating from the Tab5."""
    payload = msg.payload.strip()
    if not payload:
      return

    entity_id = None
    command = None
    service_data: Dict[str, Any] = {}

    parsed = _try_parse_json(payload)
    if isinstance(parsed, dict):
      entity_id = parsed.get("entity_id") or parsed.get("entity")
      if entity_id is not None:
        entity_id = str(entity_id).strip()
      command = _normalise_command(parsed.get("state") or parsed.get("command"))
      service_data = _extract_light_service_data(parsed)
    elif isinstance(parsed, str):
      payload = parsed.strip()

    if command is None:
      parsed_entity, parsed_command = _parse_simple_command(payload)
      if entity_id is None:
        entity_id = parsed_entity
      if command is None:
        command = parsed_command

    entity_id = self._resolve_target_entity(entity_id, self.lights)
    if not entity_id:
      _LOGGER.warning("Unhandled light command from Tab5 (unknown entity): %s", msg.payload)
      return

    if command is None:
      if service_data:
        command = "on"
      else:
        _LOGGER.warning("Unhandled light command from Tab5 (missing state): %s", msg.payload)
        return

    command = _normalise_command(command)
    if not command:
      _LOGGER.warning("Unhandled light command from Tab5: %s", msg.payload)
      return

    if command == "toggle":
      service = "toggle"
      service_payload = {"entity_id": entity_id}
    elif command == "off":
      service = "turn_off"
      service_payload = {"entity_id": entity_id}
      if "transition" in service_data:
        service_payload["transition"] = service_data["transition"]
    else:
      service = "turn_on"
      service_payload = {"entity_id": entity_id}
      service_payload.update(service_data)

    await self.hass.services.async_call(
      "light",
      service,
      service_payload,
      blocking=False,
    )

  async def _async_handle_switch_command(self, msg: ReceiveMessage) -> None:
    """Execute switch commands originating from the Tab5."""
    payload = msg.payload.strip()
    if not payload:
      return

    entity_id = None
    command = None

    parsed = _try_parse_json(payload)
    if isinstance(parsed, dict):
      entity_id = parsed.get("entity_id") or parsed.get("entity")
      if entity_id is not None:
        entity_id = str(entity_id).strip()
      command = _normalise_command(parsed.get("state") or parsed.get("command"))
    elif isinstance(parsed, str):
      payload = parsed.strip()

    if command is None:
      parsed_entity, parsed_command = _parse_simple_command(payload)
      if entity_id is None:
        entity_id = parsed_entity
      if command is None:
        command = parsed_command

    entity_id = self._resolve_target_entity(entity_id, self.switches)
    if not entity_id:
      _LOGGER.warning("Unhandled switch command from Tab5 (unknown entity): %s", msg.payload)
      return

    command = _normalise_command(command)
    if not command:
      _LOGGER.warning("Unhandled switch command from Tab5: %s", msg.payload)
      return

    service = "toggle" if command == "toggle" else "turn_on" if command == "on" else "turn_off"
    await self.hass.services.async_call(
      "switch",
      service,
      {"entity_id": entity_id},
      blocking=False,
    )

  def _resolve_target_entity(self, entity_id: Optional[str], candidates: List[str]) -> Optional[str]:
    if entity_id:
      entity_id = entity_id.strip()
      if entity_id in candidates:
        return entity_id
      return None
    if len(candidates) == 1:
      return candidates[0]
    return None

  @callback
  def _handle_state_event(self, event) -> None:
    entity_id = event.data.get("entity_id")
    new_state = event.data.get("new_state")
    if not entity_id or not new_state:
      return

    topic = self._ha_topic_for_entity(entity_id, "state")
    payload = self._build_state_payload(entity_id, new_state)
    self.hass.async_create_task(
      mqtt.async_publish(self.hass, topic, payload, qos=0, retain=True)
    )
    if _is_weather_entity(entity_id):
      weather_topic = self._ha_topic_for_entity(entity_id, "weather")
      self.hass.async_create_task(
        self._async_publish_weather_state(entity_id, new_state, retain=True)
      )

    # Live icon updates without full grid reload.
    if entity_id in self.tracked_entities:
      if _is_weather_entity(entity_id):
        icon = _weather_icon_from_state(new_state, self.hass) or ""
      else:
        icon = _extract_mdi_icon(new_state, self.hass) or ""
      if self._icon_cache.get(entity_id, "") != icon:
        self._icon_cache[entity_id] = icon
        self._schedule_icon_refresh()

  def _schedule_config_refresh(self, delays: Optional[Tuple[float, ...]] = None) -> None:
    if not self.config_topic:
      return
    if self._config_refresh_pending > 0:
      return
    if delays is None:
      delays = (6.0, 30.0, 120.0)

    self._config_refresh_pending = len(delays)
    if self._config_refresh_pending == 0:
      return

    def _refresh(_now) -> None:
      asyncio.run_coroutine_threadsafe(
        self.async_publish_config_to_device(),
        self.hass.loop,
      )
      self._config_refresh_pending -= 1
      if self._config_refresh_pending <= 0:
        self._config_refresh_handles = []
        self._config_refresh_pending = 0

    for delay in delays:
      self._config_refresh_handles.append(async_call_later(self.hass, delay, _refresh))

  def _schedule_icon_refresh(self, delay: float = 2.0) -> None:
    if not self.config_topic:
      return
    if self._icon_refresh_handle:
      return

    def _refresh(_now) -> None:
      self._icon_refresh_handle = None
      asyncio.run_coroutine_threadsafe(
        self.async_publish_config_to_device(),
        self.hass.loop,
      )

    self._icon_refresh_handle = async_call_later(self.hass, delay, _refresh)

  async def _get_weather_forecast(self, entity_id: str) -> Optional[List[Dict[str, Any]]]:
    now = dt_util.utcnow()
    cached = self._forecast_cache.get(entity_id)
    if cached and (now - cached[0]) < FORECAST_CACHE_TTL:
      return cached[1]

    forecast = await self._fetch_weather_forecast(entity_id)
    if forecast:
      self._forecast_cache[entity_id] = (now, forecast)
    return forecast

  async def _fetch_weather_forecast(self, entity_id: str) -> Optional[List[Dict[str, Any]]]:
    forecast: Optional[List[Dict[str, Any]]] = None

    if async_get_forecasts:
      try:
        result = await async_get_forecasts(self.hass, entity_id, FORECAST_TYPE)
      except TypeError:
        try:
          result = await async_get_forecasts(self.hass, entity_id)
        except Exception as err:  # pragma: no cover - optional helper
          _LOGGER.debug("Tab5 LVGL: async_get_forecasts failed for %s (%s)", entity_id, err)
          result = None
      except Exception as err:  # pragma: no cover - optional helper
        _LOGGER.debug("Tab5 LVGL: async_get_forecasts failed for %s (%s)", entity_id, err)
        result = None

      if result is not None:
        forecast = _extract_forecast_from_result(result, entity_id)

    if forecast is None:
      try:
        response = await self.hass.services.async_call(
          "weather",
          "get_forecasts",
          {"entity_id": entity_id, "type": FORECAST_TYPE},
          blocking=True,
          return_response=True,
        )
      except TypeError:
        try:
          await self.hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": entity_id, "type": FORECAST_TYPE},
            blocking=True,
          )
        except Exception as err:
          _LOGGER.debug("Tab5 LVGL: weather.get_forecasts failed for %s (%s)", entity_id, err)
        response = None
      except Exception as err:
        _LOGGER.debug("Tab5 LVGL: weather.get_forecasts failed for %s (%s)", entity_id, err)
        response = None

      if response is not None:
        forecast = _extract_forecast_from_result(response, entity_id)

    forecast = _sanitize_forecast_list(forecast)
    if not forecast:
      return None
    _apply_forecast_icons(forecast)
    if len(forecast) > FORECAST_LIMIT:
      forecast = forecast[:FORECAST_LIMIT]
    return forecast

  def _prime_icon_cache(self) -> None:
    if not self.tracked_entities:
      return
    for entity_id in self.tracked_entities:
      state = self.hass.states.get(entity_id)
      if not state:
        self._icon_cache[entity_id] = ""
        continue
      if _is_weather_entity(entity_id):
        icon = _weather_icon_from_state(state, self.hass) or ""
      else:
        icon = _extract_mdi_icon(state, self.hass) or ""
      self._icon_cache[entity_id] = icon

  def _build_state_payload(self, entity_id: str, state: State) -> str:
    if entity_id.startswith("light."):
      payload: Dict[str, Any] = {"state": state.state}
      attrs = state.attributes or {}
      supported_modes = attrs.get("supported_color_modes")
      if supported_modes:
        payload["supported_color_modes"] = list(supported_modes)
      color_mode = attrs.get("color_mode")
      if color_mode:
        payload["color_mode"] = color_mode
      brightness_pct = attrs.get("brightness_pct")
      if brightness_pct is None:
        brightness = attrs.get("brightness")
        if isinstance(brightness, (int, float)):
          brightness_pct = round(brightness / 255 * 100)
      if brightness_pct is not None:
        payload["brightness_pct"] = brightness_pct
      rgb = attrs.get("rgb_color")
      if isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
        r, g, b = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        payload["rgb_color"] = [r, g, b]
        payload["color"] = f"#{r:02X}{g:02X}{b:02X}"
      hs = attrs.get("hs_color")
      if isinstance(hs, (list, tuple)) and len(hs) >= 2:
        payload["hs_color"] = [float(hs[0]), float(hs[1])]
      return json.dumps(payload)
    return state.state.replace(",", ".")

  async def _async_publish_weather_state(self, entity_id: str, state: State, retain: bool = True) -> None:
    payload = await self._build_weather_payload(entity_id, state)
    topic = self._ha_topic_for_entity(entity_id, "weather")
    await mqtt.async_publish(self.hass, topic, payload, qos=0, retain=retain)

  async def _build_weather_payload(self, entity_id: str, state: State) -> str:
    payload = _extract_weather_payload(state, self.hass)
    forecast = payload.get("forecast")
    if not forecast:
      cached = await self._get_weather_forecast(entity_id)
      if cached:
        payload["forecast"] = cached
    return json.dumps(payload)

  def _ha_topic_for_entity(self, entity_id: str, suffix: str) -> str:
    path = entity_id.replace(".", "/")
    return f"{self.ha_prefix}/{path}/{suffix}"

  def _build_sensor_meta(self) -> List[Dict[str, str]]:
    meta: List[Dict[str, str]] = []
    for entity_id in self.sensors:
      entry: Dict[str, str] = {"entity_id": entity_id}
      state: Optional[State] = self.hass.states.get(entity_id)
      if state:
        unit = state.attributes.get("unit_of_measurement")
        name = state.name
        value = state.state
        icon = _extract_mdi_icon(state, self.hass)
        if isinstance(unit, str) and unit.strip():
          entry["unit"] = unit.strip()
        if isinstance(name, str) and name.strip():
          entry["name"] = name.strip()
        if isinstance(value, str) and value.strip():
          entry["value"] = value.strip()
        if isinstance(icon, str) and icon.strip():
          entry["icon"] = icon.strip()
      meta.append(entry)
    return meta

  def _build_weather_meta(self) -> List[Dict[str, Any]]:
    meta: List[Dict[str, Any]] = []
    for entity_id in self.weathers:
      entry: Dict[str, Any] = {"entity_id": entity_id}
      state: Optional[State] = self.hass.states.get(entity_id)
      if state:
        name = state.name
        if isinstance(name, str) and name.strip():
          entry["name"] = name.strip()
        icon = _weather_icon_from_state(state, self.hass)
        if isinstance(icon, str) and icon.strip():
          entry["icon"] = icon.strip()
      meta.append(entry)
    return meta

  def _build_entity_meta(self, entities: List[str]) -> List[Dict[str, str]]:
    meta: List[Dict[str, str]] = []
    for entity_id in entities:
      entry: Dict[str, str] = {"entity_id": entity_id}
      state: Optional[State] = self.hass.states.get(entity_id)
      if state:
        name = state.name
        value = state.state
        icon = _extract_mdi_icon(state, self.hass)
        if isinstance(name, str) and name.strip():
          entry["name"] = name.strip()
        if isinstance(value, str) and value.strip():
          entry["state"] = value.strip()
        if isinstance(icon, str) and icon.strip():
          entry["icon"] = icon.strip()
      meta.append(entry)
    return meta

  def _build_scene_meta(self) -> List[Dict[str, str]]:
    entities = list({entity for entity in self.scene_map.values() if entity})
    return self._build_entity_meta(entities)


def _unique_entities(entities: List[str]) -> List[str]:
  seen = set()
  result: List[str] = []
  for entity_id in entities:
    cleaned = (entity_id or "").strip()
    if not cleaned or cleaned in seen:
      continue
    seen.add(cleaned)
    result.append(cleaned)
  return result


def _is_weather_entity(entity_id: str) -> bool:
  return isinstance(entity_id, str) and entity_id.startswith("weather.")


def _split_weather_entities(entities: List[str]) -> Tuple[List[str], List[str]]:
  weathers: List[str] = []
  sensors: List[str] = []
  for entity_id in entities:
    if _is_weather_entity(entity_id):
      weathers.append(entity_id)
    else:
      sensors.append(entity_id)
  return _unique_entities(weathers), _unique_entities(sensors)


def _try_parse_json(payload: str) -> Any:
  try:
    return json.loads(payload)
  except (ValueError, TypeError):
    return None


def _normalise_command(value: Any) -> Optional[str]:
  if value is None:
    return None
  text = str(value).strip().lower()
  if not text:
    return None
  if text in ("on", "off", "toggle"):
    return text
  if text in ("1", "true", "yes"):
    return "on"
  if text in ("0", "false", "no"):
    return "off"
  return None


def _parse_simple_command(payload: str) -> Tuple[Optional[str], Optional[str]]:
  text = (payload or "").strip()
  if not text:
    return None, None
  if " " in text:
    first, rest = text.split(None, 1)
    if "." in first and rest.strip():
      return first.strip(), rest.strip()
  for separator in (":", "="):
    if separator in text:
      left, right = text.split(separator, 1)
      if "." in left.strip() and right.strip():
        return left.strip(), right.strip()
  return None, text


def _extract_light_service_data(payload: Dict[str, Any]) -> Dict[str, Any]:
  data: Dict[str, Any] = {}
  for key in LIGHT_SERVICE_FIELDS:
    if key in payload and payload[key] is not None:
      data[key] = payload[key]
  return data


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
  try:
    result = int(value)
  except (TypeError, ValueError):
    result = default
  if result < minimum:
    return minimum
  if result > maximum:
    return maximum
  return result


def _normalise_topic(value: Optional[str], default: str) -> str:
  result = (value or "").strip() or default
  while result.endswith("/"):
    result = result[:-1]
  return result or default


def _fallback_icon_from_state(state: State) -> Optional[str]:
  if not state:
    return None
  entity_id = state.entity_id or ""
  domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
  attrs = state.attributes or {}
  device_class = str(attrs.get("device_class") or "").strip().lower()
  unit = str(attrs.get("unit_of_measurement") or "").strip()
  unit_norm = unit.lower().replace(" ", "")

  if domain == "light":
    return "mdi:lightbulb"
  if domain == "switch":
    return "mdi:toggle-switch"
  if domain == "scene":
    return "mdi:palette"

  if domain == "sensor":
    device_class_icons = {
      "temperature": "mdi:thermometer",
      "humidity": "mdi:water-percent",
      "power": "mdi:flash",
      "apparent_power": "mdi:flash",
      "voltage": "mdi:flash",
      "current": "mdi:flash",
      "energy": "mdi:lightning-bolt",
      "battery": "mdi:battery",
      "pressure": "mdi:gauge",
    }
    if device_class in device_class_icons:
      return device_class_icons[device_class]

    if unit_norm in {"°c", "°f", "c", "f", "degc", "degf"}:
      return "mdi:thermometer"
    if unit_norm in {"w", "kw", "mw", "va", "kva", "mva"}:
      return "mdi:flash"
    if unit_norm in {"wh", "kwh", "mwh"}:
      return "mdi:lightning-bolt"

  return None


def _extract_mdi_icon(state: State, hass: Optional[HomeAssistant] = None) -> Optional[str]:
  if not state:
    return None
  raw_icon = state.attributes.get("icon")
  icon = raw_icon.strip() if isinstance(raw_icon, str) else ""
  if not icon and hass and icon_for_entity:
    try:
      # HA 2025+ typically supports state kwarg.
      icon = icon_for_entity(hass, state.entity_id, state=state)
    except TypeError:
      try:
        # Older signature: (hass, entity_id, state)
        icon = icon_for_entity(hass, state.entity_id, state)
      except TypeError:
        try:
          # Older signature: (hass, entity_id)
          icon = icon_for_entity(hass, state.entity_id)
        except TypeError:
          try:
            # Legacy fallback: (hass, state)
            icon = icon_for_entity(hass, state)
          except Exception:
            icon = None
        except Exception:
          icon = None
      except Exception:
        icon = None
    except Exception:
      icon = None
  if not icon:
    icon = _fallback_icon_from_state(state)
  if not isinstance(icon, str):
    return None
  icon = icon.strip()
  if not icon:
    return None
  # Accept standard MDI prefixes (mdi:home, mdi-home) or bare icon names.
  if ":" in icon and not icon.startswith("mdi:"):
    return None
  if icon.startswith("mdi-"):
    return "mdi:" + icon[4:]
  return icon


def _normalize_weather_value(value: Any) -> Any:
  if isinstance(value, datetime):
    try:
      return dt_util.as_utc(value).isoformat()
    except Exception:
      return value.isoformat()
  if isinstance(value, date):
    return value.isoformat()
  return value


def _sanitize_forecast_list(value: Any) -> Optional[List[Dict[str, Any]]]:
  if not isinstance(value, list):
    return None
  cleaned: List[Dict[str, Any]] = []
  for item in value:
    if not isinstance(item, dict):
      continue
    out: Dict[str, Any] = {}
    for key, val in item.items():
      if val is None:
        continue
      out[key] = _normalize_weather_value(val)
    if out:
      cleaned.append(out)
  return cleaned if cleaned else None


_WEATHER_ICON_MAP = {
  "clear-night": "mdi:weather-night",
  "cloudy": "mdi:weather-cloudy",
  "exceptional": "mdi:alert-circle-outline",
  "fog": "mdi:weather-fog",
  "hail": "mdi:weather-hail",
  "lightning": "mdi:weather-lightning",
  "lightning-rainy": "mdi:weather-lightning-rainy",
  "partlycloudy": "mdi:weather-partly-cloudy",
  "pouring": "mdi:weather-pouring",
  "rainy": "mdi:weather-rainy",
  "snowy": "mdi:weather-snowy",
  "snowy-rainy": "mdi:weather-snowy-rainy",
  "sunny": "mdi:weather-sunny",
  "windy": "mdi:weather-windy",
  "windy-variant": "mdi:weather-windy-variant",
}


def _apply_forecast_icons(forecast: List[Dict[str, Any]]) -> None:
  for entry in forecast:
    if not isinstance(entry, dict):
      continue
    icon = entry.get("icon")
    if isinstance(icon, str) and icon.strip():
      continue
    condition = entry.get("condition") or entry.get("state")
    if not isinstance(condition, str):
      continue
    key = condition.strip().lower()
    if key in _WEATHER_ICON_MAP:
      entry["icon"] = _WEATHER_ICON_MAP[key]


def _extract_forecast_from_result(result: Any, entity_id: str) -> Optional[List[Dict[str, Any]]]:
  if isinstance(result, list):
    return result
  if not isinstance(result, dict):
    return None
  if isinstance(result.get("forecast"), list):
    return result.get("forecast")
  entry = result.get(entity_id) or result.get(entity_id.lower()) or result.get(entity_id.upper())
  if isinstance(entry, list):
    return entry
  if isinstance(entry, dict):
    if isinstance(entry.get("forecast"), list):
      return entry.get("forecast")
    if isinstance(entry.get("forecasts"), list):
      return entry.get("forecasts")
  return None


def _weather_icon_from_state(state: State, hass: Optional[HomeAssistant] = None) -> Optional[str]:
  if not state:
    return None
  icon = _extract_mdi_icon(state, hass)
  if icon:
    return icon
  attrs = state.attributes or {}
  condition = attrs.get("condition") or state.state
  if isinstance(condition, str):
    key = condition.strip().lower()
    if key in _WEATHER_ICON_MAP:
      return _WEATHER_ICON_MAP[key]
  return None


def _extract_weather_payload(state: State, hass: Optional[HomeAssistant] = None) -> Dict[str, Any]:
  attrs = state.attributes or {}
  payload: Dict[str, Any] = {"state": state.state}

  name = state.name
  if isinstance(name, str) and name.strip():
    payload["name"] = name.strip()

  icon = _weather_icon_from_state(state, hass)
  if isinstance(icon, str) and icon.strip():
    payload["icon"] = icon.strip()

  for key in (
    "temperature",
    "apparent_temperature",
    "dew_point",
    "humidity",
    "pressure",
    "wind_speed",
    "wind_bearing",
    "wind_gust_speed",
    "visibility",
    "ozone",
    "uv_index",
    "cloud_coverage",
    "precipitation",
    "precipitation_probability",
  ):
    if key in attrs and attrs[key] is not None:
      payload[key] = _normalize_weather_value(attrs[key])

  units: Dict[str, Any] = {}
  for unit_key, unit_name in (
    ("temperature_unit", "temperature"),
    ("pressure_unit", "pressure"),
    ("wind_speed_unit", "wind_speed"),
    ("visibility_unit", "visibility"),
    ("precipitation_unit", "precipitation"),
  ):
    unit_val = attrs.get(unit_key)
    if unit_val:
      units[unit_name] = unit_val
  if units:
    payload["units"] = units

  attribution = attrs.get("attribution")
  if isinstance(attribution, str) and attribution.strip():
    payload["attribution"] = attribution.strip()

  forecast = _sanitize_forecast_list(attrs.get("forecast"))
  if forecast:
    _apply_forecast_icons(forecast)
    if len(forecast) > FORECAST_LIMIT:
      forecast = forecast[:FORECAST_LIMIT]
    payload["forecast"] = forecast

  return payload


async def _async_process_bridge_config(hass: HomeAssistant, payload: Dict[str, Any]) -> None:
  try:
    data = _payload_to_entry_data(payload)
  except ValueError as err:
    _LOGGER.warning("Tab5 LVGL: Konfigurationspayload ignoriert (%s)", err)
    return

  device_id = data.get(CONF_DEVICE_ID)
  entry = _find_entry_by_device_id(hass, device_id)

  if entry:
    # Gerät ist bereits mit dieser Integration verbunden -> nichts zu tun
    return

  fallback = _find_entry_by_base(hass, data.get(CONF_BASE_TOPIC))
  if fallback:
    new_data = dict(fallback.data)
    changed = False
    if device_id and new_data.get(CONF_DEVICE_ID) != device_id:
      new_data[CONF_DEVICE_ID] = device_id
      changed = True
    if not changed:
      return

    _LOGGER.info("Tab5 LVGL: verknuepfe Bridge %s mit bestehender Integration", device_id)
    hass.config_entries.async_update_entry(
      fallback,
      data=new_data,
      title=_entry_title(new_data),
      unique_id=device_id or fallback.unique_id,
    )
    await hass.config_entries.async_reload(fallback.entry_id)
    return

  _LOGGER.info("Tab5 LVGL: neue Bridge entdeckt (%s) - erstelle Integration", device_id)
  hass.async_create_task(
    hass.config_entries.flow.async_init(
      DOMAIN,
      context={"source": config_entries.SOURCE_IMPORT},
      data=data,
    )
  )


def _payload_to_entry_data(payload: Dict[str, Any]) -> Dict[str, Any]:
  device_id = payload.get("device_id")
  if not device_id:
    raise ValueError("missing_device_id")

  base = _normalise_topic(payload.get("base_topic"), DEFAULT_BASE)
  prefix = _normalise_topic(payload.get("ha_prefix"), DEFAULT_PREFIX)

  sensors_raw = payload.get("sensors") or []
  if not isinstance(sensors_raw, list):
    raise ValueError("invalid_sensors")
  sensors = [str(item).strip() for item in sensors_raw if str(item).strip()]

  weathers_raw = payload.get("weathers") or []
  if not isinstance(weathers_raw, list):
    raise ValueError("invalid_weathers")
  weathers = [str(item).strip() for item in weathers_raw if str(item).strip()]
  if weathers:
    sensors = _unique_entities(sensors + weathers)

  lights_raw = payload.get("lights") or []
  if not isinstance(lights_raw, list):
    raise ValueError("invalid_lights")
  lights = [str(item).strip() for item in lights_raw if str(item).strip()]

  switches_raw = payload.get("switches") or []
  if not isinstance(switches_raw, list):
    raise ValueError("invalid_switches")
  switches = [str(item).strip() for item in switches_raw if str(item).strip()]

  scene_map_raw = payload.get("scene_map") or {}
  if not isinstance(scene_map_raw, dict):
    raise ValueError("invalid_scene_map")
  scene_map: Dict[str, str] = {}
  for alias, entity in scene_map_raw.items():
    if not alias or not entity:
      continue
    scene_map[str(alias).lower()] = str(entity)

  return {
    CONF_DEVICE_ID: device_id,
    CONF_BASE_TOPIC: base,
    CONF_HA_PREFIX: prefix,
    CONF_SENSORS: sensors,
    CONF_LIGHTS: lights,
    CONF_SWITCHES: switches,
    CONF_SCENE_MAP: scene_map,
  }


def _find_entry_by_device_id(hass: HomeAssistant, device_id: Optional[str]) -> Optional[ConfigEntry]:
  if not device_id:
    return None
  for entry in hass.config_entries.async_entries(DOMAIN):
    if entry.data.get(CONF_DEVICE_ID) == device_id or entry.unique_id == device_id:
      return entry
  return None


def _find_entry_by_base(hass: HomeAssistant, base_topic: Optional[str]) -> Optional[ConfigEntry]:
  if not base_topic:
    return None
  for entry in hass.config_entries.async_entries(DOMAIN):
    if entry.data.get(CONF_BASE_TOPIC) == base_topic:
      return entry
  return None


def _entry_title(data: Dict[str, Any]) -> str:
  device_id = data.get(CONF_DEVICE_ID)
  if device_id:
    suffix = device_id[-4:].upper()
    return f"Tab5 {suffix}"
  return "Tab5 LVGL"
