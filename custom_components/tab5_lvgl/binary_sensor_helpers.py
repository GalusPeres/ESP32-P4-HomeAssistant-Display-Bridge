"""Protocol and migration helpers for Home Assistant binary sensors."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping


BINARY_SENSOR_STATES = frozenset({"on", "off", "unknown", "unavailable"})


def split_binary_sensor_entities(
  entity_ids: Iterable[Any],
) -> tuple[list[str], list[str]]:
  """Separate binary sensors from a legacy generic sensor selection."""
  binary_sensors: list[str] = []
  sensors: list[str] = []
  seen_binary: set[str] = set()
  seen_sensors: set[str] = set()

  for raw_entity_id in entity_ids:
    entity_id = str(raw_entity_id or "").strip()
    if not entity_id:
      continue
    if entity_id.startswith("binary_sensor."):
      if entity_id not in seen_binary:
        seen_binary.add(entity_id)
        binary_sensors.append(entity_id)
    elif entity_id not in seen_sensors:
      seen_sensors.add(entity_id)
      sensors.append(entity_id)
  return binary_sensors, sensors


def migrate_binary_sensor_config(
  data: Mapping[str, Any] | None,
  options: Mapping[str, Any] | None,
  *,
  sensor_key: str = "sensors",
  binary_sensor_key: str = "binary_sensors",
) -> tuple[dict[str, Any], dict[str, Any], bool]:
  """Move legacy ``binary_sensor.*`` selections to their dedicated list.

  Both config-entry data and options are migrated independently so an explicit
  empty options selection keeps overriding data after the migration.
  """
  migrated_data = dict(data or {})
  migrated_options = dict(options or {})
  changed = False

  for storage, is_options in (
    (migrated_data, False),
    (migrated_options, True),
  ):
    raw_sensors = storage.get(sensor_key)
    if not isinstance(raw_sensors, (list, tuple)):
      continue

    legacy_binary, sensors = split_binary_sensor_entities(raw_sensors)
    explicit_binary = storage.get(binary_sensor_key)
    if isinstance(explicit_binary, (list, tuple)):
      configured_binary, _ = split_binary_sensor_entities(explicit_binary)
    else:
      configured_binary = []
    binary_sensors = _unique(configured_binary + legacy_binary)

    if list(raw_sensors) != sensors:
      storage[sensor_key] = sensors
      changed = True
    # Options override entry data as a complete selection. When an older
    # options payload contains the generic sensor field, persist an explicit
    # (possibly empty) binary selection too; otherwise a binary sensor removed
    # through the old UI could be resurrected from entry data after migration.
    owns_legacy_selection = is_options and sensor_key in storage
    if (
      (legacy_binary or owns_legacy_selection)
      and storage.get(binary_sensor_key) != binary_sensors
    ):
      storage[binary_sensor_key] = binary_sensors
      changed = True

  return migrated_data, migrated_options, changed


def build_binary_sensor_state_payload(
  state_value: Any,
  attributes: Mapping[str, Any] | None,
  last_changed: Any = None,
  *,
  icon: Any = None,
) -> dict[str, Any]:
  """Build the stable state contract consumed by HomeTiles firmware."""
  if state_value is None:
    return {
      "state": None,
      "available": None,
      "device_class": None,
      "last_changed": None,
    }

  attrs = attributes or {}
  device_class = _normalise_device_class(attrs.get("device_class"))
  state = str(state_value or "").strip().lower()
  if state not in BINARY_SENSOR_STATES:
    state = "unknown"
  payload = {
    "state": state,
    "available": state != "unavailable",
    "device_class": device_class,
    "last_changed": _unix_seconds(last_changed),
  }
  normalised_icon = _normalise_icon(icon)
  if normalised_icon:
    payload["icon"] = normalised_icon
  return payload


def build_binary_sensor_meta_entry(
  entity_id: Any,
  state_value: Any,
  attributes: Mapping[str, Any] | None,
  last_changed: Any = None,
  *,
  name: Any = None,
  icon: Any = None,
) -> dict[str, Any]:
  """Build one config metadata entry with an initial binary state."""
  entry = {"entity_id": str(entity_id or "").strip()}
  entry.update(
    build_binary_sensor_state_payload(
      state_value,
      attributes,
      last_changed,
      icon=icon,
    )
  )
  if isinstance(name, str) and name.strip():
    entry["name"] = name.strip()
  return entry


def _normalise_device_class(value: Any) -> str | None:
  if hasattr(value, "value"):
    value = value.value
  if not isinstance(value, str):
    return None
  value = value.strip().lower()
  return value[:64] or None


def _normalise_icon(value: Any) -> str | None:
  if not isinstance(value, str):
    return None
  value = value.strip()
  return value[:39] or None


def _unix_seconds(value: Any) -> int | None:
  if not isinstance(value, datetime):
    return None
  try:
    return int(value.timestamp())
  except (OverflowError, OSError, ValueError):
    return None


def _unique(values: Iterable[str]) -> list[str]:
  result: list[str] = []
  seen: set[str] = set()
  for value in values:
    if value in seen:
      continue
    seen.add(value)
    result.append(value)
  return result
