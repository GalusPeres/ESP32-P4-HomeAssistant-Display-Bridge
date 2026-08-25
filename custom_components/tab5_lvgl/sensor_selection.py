"""Pure helpers for separating configured and integration-owned sensors."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any


BATTERY_SENSOR_NAME = "Batterie SoC"
EXTERNAL_TEMPERATURE_SENSOR_NAME = "Externe Temperatur"

INTERNAL_SENSOR_ENTITY_ID_TARGETS = (
    ("_battery_soc", "sensor.tab5_internal_battery_soc"),
    ("_external_temperature", "sensor.tab5_external_temperature"),
)

_INTERNAL_SENSOR_NAME_VARIANTS = (
    BATTERY_SENSOR_NAME,
    "Battery SoC",
    EXTERNAL_TEMPERATURE_SENSOR_NAME,
    "External Temperature",
)


def runtime_sensor_entity_id_candidates(
    device_labels: Iterable[str],
    owned_entity_ids: Iterable[str],
    announced_entity_ids: Iterable[str],
    slugify_func: Callable[[str], str],
) -> set[str]:
    """Return exact entity IDs that can be generated for runtime sensors."""
    candidates = {
        entity_id
        for _, entity_id in INTERNAL_SENSOR_ENTITY_ID_TARGETS
    }
    candidates.update(_normalise_entity_ids(owned_entity_ids))
    candidates.update(_normalise_entity_ids(announced_entity_ids))

    for sensor_name in _INTERNAL_SENSOR_NAME_VARIANTS:
        object_id = slugify_func(sensor_name)
        if object_id:
            candidates.add(f"sensor.{object_id}")

    for raw_label in device_labels:
        label = str(raw_label or "").strip()
        if not label:
            continue
        for sensor_name in _INTERNAL_SENSOR_NAME_VARIANTS:
            object_id = slugify_func(f"{label} {sensor_name}")
            if object_id:
                candidates.add(f"sensor.{object_id}")
    return candidates


def filter_runtime_sensor_entities(
    entity_ids: Iterable[str],
    runtime_entity_ids: Iterable[str],
) -> list[str]:
    """Remove runtime-owned IDs while preserving configured sensor order."""
    blocked = set(_normalise_entity_ids(runtime_entity_ids))
    result: list[str] = []
    seen: set[str] = set()
    for raw_entity_id in entity_ids:
        entity_id = str(raw_entity_id or "").strip()
        if not entity_id or entity_id in blocked or entity_id in seen:
            continue
        seen.add(entity_id)
        result.append(entity_id)
    return result


def clean_stored_sensor_selections(
    data: Mapping[str, Any] | None,
    options: Mapping[str, Any] | None,
    sensor_key: str,
    runtime_entity_ids: Iterable[str],
) -> tuple[dict[str, Any], dict[str, Any], bool, bool, int]:
    """Clean runtime-only sensors from config-entry data and options."""
    cleaned_data = dict(data or {})
    cleaned_options = dict(options or {})
    data_changed = False
    options_changed = False
    removed_count = 0

    for storage, is_options in (
        (cleaned_data, False),
        (cleaned_options, True),
    ):
        raw_entity_ids = storage.get(sensor_key)
        if not isinstance(raw_entity_ids, (list, tuple)):
            continue
        cleaned = filter_runtime_sensor_entities(
            raw_entity_ids, runtime_entity_ids
        )
        if list(raw_entity_ids) == cleaned:
            continue
        removed_count += len(raw_entity_ids) - len(cleaned)
        storage[sensor_key] = cleaned
        if is_options:
            options_changed = True
        else:
            data_changed = True

    return (
        cleaned_data,
        cleaned_options,
        data_changed,
        options_changed,
        removed_count,
    )


def should_import_feedback_selection(
    data: Mapping[str, Any] | None,
    options: Mapping[str, Any] | None,
    key: str,
    incoming_value: Any,
) -> bool:
    """Return whether feedback may initialize a previously absent selection."""
    return (
        key not in (data or {})
        and key not in (options or {})
        and bool(incoming_value)
    )


def _normalise_entity_ids(entity_ids: Iterable[str]) -> set[str]:
    return {
        str(entity_id or "").strip()
        for entity_id in entity_ids
        if str(entity_id or "").strip()
    }
