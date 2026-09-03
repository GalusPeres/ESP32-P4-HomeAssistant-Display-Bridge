"""Dependency-free helpers for the HomeTiles binary history protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Iterable, Mapping, Optional


BINARY_HISTORY_VERSION = 1
BINARY_HISTORY_KIND = "binary"
BINARY_HISTORY_DEFAULT_HOURS = 24
BINARY_HISTORY_ALLOWED_HOURS = frozenset({24, 168})
BINARY_HISTORY_DEFAULT_MAX_TRANSITIONS = 48
BINARY_HISTORY_MIN_TRANSITIONS = 2
BINARY_HISTORY_MAX_TRANSITIONS = 96
BINARY_HISTORY_STATES = frozenset({"on", "off", "unknown", "unavailable"})


@dataclass(frozen=True)
class BinaryHistoryRequest:
  """Validated binary history request limits."""

  hours: int
  max_transitions: int


class BinaryHistoryRequestError(ValueError):
  """A stable, publishable binary history request error."""

  def __init__(self, code: str) -> None:
    super().__init__(code)
    self.code = code


def parse_binary_history_request(
  payload: Mapping[str, Any],
) -> BinaryHistoryRequest:
  """Validate the versioned binary history request contract."""
  version = _parse_integer(payload.get("version"))
  if version != BINARY_HISTORY_VERSION:
    raise BinaryHistoryRequestError("unsupported_version")

  hours_value = payload.get("hours")
  hours = (
    BINARY_HISTORY_DEFAULT_HOURS
    if hours_value is None
    else _parse_integer(hours_value)
  )
  if hours not in BINARY_HISTORY_ALLOWED_HOURS:
    raise BinaryHistoryRequestError("invalid_hours")

  max_value = payload.get("max_transitions")
  max_transitions = (
    BINARY_HISTORY_DEFAULT_MAX_TRANSITIONS
    if max_value is None
    else _parse_integer(max_value)
  )
  if (
    max_transitions is None
    or max_transitions < BINARY_HISTORY_MIN_TRANSITIONS
    or max_transitions > BINARY_HISTORY_MAX_TRANSITIONS
  ):
    raise BinaryHistoryRequestError("invalid_max_transitions")

  return BinaryHistoryRequest(hours=hours, max_transitions=max_transitions)


def build_binary_history_error(entity_id: Any, code: str) -> dict[str, Any]:
  """Build a compact error on the same versioned response channel."""
  return {
    "version": BINARY_HISTORY_VERSION,
    "kind": BINARY_HISTORY_KIND,
    "entity_id": str(entity_id or "").strip(),
    "error": code,
  }


def build_binary_history_response(
  entity_id: str,
  history_states: Iterable[Any],
  current_state: Any,
  range_start: datetime,
  range_end: datetime,
  hours: int,
  max_transitions: int,
  history_available: bool = True,
) -> dict[str, Any]:
  """Build bounded binary-state segments and activity from recorder states.

  Segments cover the complete requested range. If the exact history exceeds
  the requested limit, the oldest omitted interval becomes ``unknown`` and
  the most recent exact segments are retained. This avoids inventing a state
  while keeping the payload bounded for embedded consumers.
  """
  start = _unix_seconds(range_start)
  end = _unix_seconds(range_end)
  if start is None or end is None or end <= start:
    raise ValueError("range_end must be later than range_start")
  if (
    max_transitions < BINARY_HISTORY_MIN_TRANSITIONS
    or max_transitions > BINARY_HISTORY_MAX_TRANSITIONS
  ):
    raise ValueError("max_transitions is outside the protocol bounds")

  records: list[tuple[int, int, str]] = []
  for order, state in enumerate(history_states):
    timestamp = _state_timestamp(state)
    if timestamp is None or timestamp >= end:
      continue
    records.append(
      (timestamp, order, _normalise_binary_state(_state_value(state)))
    )

  has_history_records = bool(records)
  current_timestamp = _state_timestamp(current_state)
  if (
    has_history_records
    and current_state is not None
    and current_timestamp is not None
    and current_timestamp < end
  ):
    records.append(
      (
        current_timestamp,
        len(records),
        _normalise_binary_state(_state_value(current_state)),
      )
    )

  records.sort(key=lambda item: (item[0], item[1]))
  records = _collapse_same_timestamp(records)

  initial_state = "unknown"
  if not has_history_records and current_state is not None:
    initial_state = _normalise_binary_state(_state_value(current_state))

  transitions: list[tuple[int, str]] = []
  for timestamp, _, state in records:
    if timestamp <= start:
      initial_state = state
      continue
    previous_state = transitions[-1][1] if transitions else initial_state
    if state != previous_state:
      transitions.append((timestamp, state))

  exact_segments = _segments_from_transitions(start, end, initial_state, transitions)
  segments = _bound_segments(exact_segments, start, end, max_transitions)
  activity = [
    {"timestamp": timestamp, "state": state}
    for timestamp, state in transitions[-max_transitions:]
  ]

  if current_state is None:
    current = None
    available = None
  else:
    current = _normalise_binary_state(_state_value(current_state))
    available = current != "unavailable"
  attributes = _state_attributes(current_state)

  return {
    "version": BINARY_HISTORY_VERSION,
    "kind": BINARY_HISTORY_KIND,
    "entity_id": entity_id,
    "hours": hours,
    "range_start": start,
    "range_end": end,
    "history_available": bool(history_available),
    "current": current,
    "available": available,
    "device_class": _normalise_device_class(attributes.get("device_class")),
    "last_changed": current_timestamp,
    "segments": segments,
    "activity": activity,
  }


def _segments_from_transitions(
  start: int,
  end: int,
  initial_state: str,
  transitions: list[tuple[int, str]],
) -> list[dict[str, Any]]:
  segments: list[dict[str, Any]] = []
  segment_start = start
  state = initial_state
  for timestamp, next_state in transitions:
    if timestamp > segment_start:
      segments.append({"start": segment_start, "end": timestamp, "state": state})
    segment_start = timestamp
    state = next_state
  if segment_start < end:
    segments.append({"start": segment_start, "end": end, "state": state})
  return segments


def _bound_segments(
  segments: list[dict[str, Any]],
  start: int,
  end: int,
  limit: int,
) -> list[dict[str, Any]]:
  if len(segments) <= limit:
    return segments
  recent = [dict(segment) for segment in segments[-(limit - 1):]]
  retained_start = int(recent[0]["start"])
  if recent[0]["state"] == "unknown":
    recent[0]["start"] = start
    return recent
  return [
    {"start": start, "end": retained_start, "state": "unknown"},
    *recent,
  ]


def _collapse_same_timestamp(
  records: list[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
  collapsed: list[tuple[int, int, str]] = []
  for record in records:
    if collapsed and record[0] == collapsed[-1][0]:
      collapsed[-1] = record
    else:
      collapsed.append(record)
  return collapsed


def _state_value(state: Any) -> Any:
  if state is None:
    return None
  if isinstance(state, Mapping):
    return state.get("state")
  return getattr(state, "state", None)


def _state_attributes(state: Any) -> Mapping[str, Any]:
  if state is None:
    return {}
  attributes = (
    state.get("attributes")
    if isinstance(state, Mapping)
    else getattr(state, "attributes", None)
  )
  return attributes if isinstance(attributes, Mapping) else {}


def _state_timestamp(state: Any) -> Optional[int]:
  if state is None:
    return None
  if isinstance(state, Mapping):
    value = state.get("last_changed")
    if value is None:
      value = state.get("last_updated")
  else:
    value = getattr(state, "last_changed", None)
    if value is None:
      value = getattr(state, "last_updated", None)
  return _unix_seconds(value)


def _normalise_binary_state(value: Any) -> str:
  state = str(value or "").strip().lower()
  return state if state in BINARY_HISTORY_STATES else "unknown"


def _normalise_device_class(value: Any) -> Optional[str]:
  if hasattr(value, "value"):
    value = value.value
  if not isinstance(value, str):
    return None
  device_class = value.strip().lower()
  return device_class[:64] or None


def _unix_seconds(value: Any) -> Optional[int]:
  if isinstance(value, bool) or value is None:
    return None
  if isinstance(value, datetime):
    moment = (
      value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    )
    try:
      return int(moment.timestamp())
    except (OverflowError, OSError, ValueError):
      return None
  if isinstance(value, (int, float)):
    number = float(value)
    return int(number) if math.isfinite(number) else None
  if isinstance(value, str):
    text = value.strip()
    if not text:
      return None
    try:
      moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
      return None
    if moment.tzinfo is None:
      moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())
  return None


def _parse_integer(value: Any) -> Optional[int]:
  if isinstance(value, bool) or value is None:
    return None
  if isinstance(value, int):
    return value
  if isinstance(value, float):
    return int(value) if math.isfinite(value) and value.is_integer() else None
  if isinstance(value, str):
    text = value.strip()
    if not text:
      return None
    try:
      return int(text, 10)
    except ValueError:
      return None
  return None
