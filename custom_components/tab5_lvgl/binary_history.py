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
BINARY_HISTORY_TIMELINE_POINTS = 768
BINARY_HISTORY_TIMELINE_ENCODING = "2bit-hex"
BINARY_HISTORY_RECORDER_PAGE_SIZE = 256
BINARY_HISTORY_RECORDER_MAX_CHANGES = 8192
BINARY_HISTORY_RECORDER_RECENT_ROWS = BINARY_HISTORY_MAX_TRANSITIONS * 4 + 1
BINARY_HISTORY_STATES = frozenset({"on", "off", "unknown", "unavailable"})
_BINARY_STATE_CODES = {"off": 0, "on": 1, "unknown": 2, "unavailable": 3}
_BINARY_CODE_STATES = ("off", "on", "unknown", "unavailable")
_BINARY_TIMELINE_PRIORITY = {"off": 1, "unknown": 2, "unavailable": 3, "on": 4}


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
  history_complete: bool = True,
  history_complete_until: Any = None,
) -> dict[str, Any]:
  """Build bounded binary-state segments and activity from recorder states.

  The compact timeline covers the complete requested range independently of
  the bounded recent activity list. Short active intervals win their display
  cell so presence is not lost when many transitions share one pixel.
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

  timeline_transitions = list(transitions)
  complete_until = _unix_seconds(history_complete_until)
  if not history_complete:
    if complete_until is None:
      complete_until = transitions[-1][0] if transitions else start
    complete_until = max(start, min(end, complete_until))
    known_tail: list[tuple[int, str]] = []
    for timestamp, _, state in records:
      if timestamp <= complete_until or timestamp >= end:
        continue
      if not known_tail or known_tail[-1][1] != state:
        known_tail.append((timestamp, state))
    timeline_transitions = [
      transition
      for transition in timeline_transitions
      if transition[0] <= complete_until
    ]
    state_at_cutoff = (
      timeline_transitions[-1][1] if timeline_transitions else initial_state
    )
    current_value = (
      _normalise_binary_state(_state_value(current_state))
      if current_state is not None
      else None
    )
    current_continues_from_cutoff = (
      current_value is not None
      and current_timestamp is not None
      and current_timestamp <= complete_until
      and current_value == state_at_cutoff
    )
    if complete_until < end and not current_continues_from_cutoff:
      if state_at_cutoff != "unknown":
        timeline_transitions.append((complete_until, "unknown"))
      for timestamp, state in known_tail:
        if timestamp >= end:
          continue
        if not timeline_transitions or timeline_transitions[-1][1] != state:
          timeline_transitions.append((timestamp, state))
      if (
        current_value is not None
        and current_timestamp is not None
        and complete_until <= current_timestamp < end
        and (
          not timeline_transitions
          or timeline_transitions[-1][1] != current_value
        )
      ):
        timeline_transitions.append((current_timestamp, current_value))

  exact_segments = _segments_from_transitions(
    start, end, initial_state, timeline_transitions
  )
  timeline_codes = _timeline_codes_from_segments(
    exact_segments, start, end, BINARY_HISTORY_TIMELINE_POINTS
  )
  segments = _bound_segments(
    exact_segments,
    start,
    end,
    max_transitions,
    timeline_codes,
  )
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
    "timeline_points": BINARY_HISTORY_TIMELINE_POINTS,
    "timeline_encoding": BINARY_HISTORY_TIMELINE_ENCODING,
    "timeline_data": _encode_timeline_codes(timeline_codes),
    "timeline_complete": bool(history_available and history_complete),
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
  timeline_codes: list[int],
) -> list[dict[str, Any]]:
  if len(segments) <= limit:
    return segments
  reduced: list[dict[str, Any]] = []
  point_count = len(timeline_codes)
  for bucket in range(limit):
    point_start = (bucket * point_count) // limit
    point_end = ((bucket + 1) * point_count) // limit
    if point_end <= point_start:
      point_end = point_start + 1
    selected = max(
      timeline_codes[point_start:point_end],
      key=lambda code: _BINARY_TIMELINE_PRIORITY[_BINARY_CODE_STATES[code]],
    )
    segment_start = start + ((end - start) * bucket) // limit
    segment_end = start + ((end - start) * (bucket + 1)) // limit
    state = _BINARY_CODE_STATES[selected]
    if reduced and reduced[-1]["state"] == state:
      reduced[-1]["end"] = segment_end
    else:
      reduced.append(
        {"start": segment_start, "end": segment_end, "state": state}
      )
  return reduced


def _timeline_codes_from_segments(
  segments: list[dict[str, Any]],
  start: int,
  end: int,
  points: int,
) -> list[int]:
  codes = [_BINARY_STATE_CODES["unknown"]] * points
  priorities = [0] * points
  span = end - start
  for segment in segments:
    segment_start = max(start, int(segment["start"]))
    segment_end = min(end, int(segment["end"]))
    if segment_end <= segment_start:
      continue
    state = _normalise_binary_state(segment.get("state"))
    code = _BINARY_STATE_CODES[state]
    priority = _BINARY_TIMELINE_PRIORITY[state]
    first = ((segment_start - start) * points) // span
    last = (((segment_end - start) * points) - 1) // span
    first = max(0, min(points - 1, first))
    last = max(first, min(points - 1, last))
    for index in range(first, last + 1):
      if priority > priorities[index]:
        codes[index] = code
        priorities[index] = priority
  return codes


def _encode_timeline_codes(codes: list[int]) -> str:
  packed = bytearray((len(codes) + 3) // 4)
  for index, code in enumerate(codes):
    shift = 6 - ((index % 4) * 2)
    packed[index // 4] |= (code & 0x03) << shift
  return packed.hex()


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
