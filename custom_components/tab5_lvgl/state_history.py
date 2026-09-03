"""Bounded history helpers for Home Assistant sensors with textual states."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
from typing import Any, Callable, Iterable, Mapping, Optional


STATE_HISTORY_VERSION = 1
STATE_HISTORY_KIND = "state"
STATE_HISTORY_DEFAULT_HOURS = 24
STATE_HISTORY_ALLOWED_HOURS = frozenset({24, 168})
STATE_HISTORY_DEFAULT_MAX_TRANSITIONS = 48
STATE_HISTORY_MIN_TRANSITIONS = 2
STATE_HISTORY_MAX_TRANSITIONS = 96
STATE_HISTORY_TIMELINE_POINTS = 768
STATE_HISTORY_TIMELINE_ENCODING = "palette4-hex"
STATE_HISTORY_MAX_PALETTE_ENTRIES = 16
STATE_HISTORY_MAX_STATE_BYTES = 32
STATE_HISTORY_MAX_CURRENT_BYTES = 255
STATE_HISTORY_MAX_RESPONSE_BYTES = 20000
STATE_HISTORY_RECORDER_PAGE_SIZE = 256
STATE_HISTORY_RECORDER_MAX_CHANGES = 8192
STATE_HISTORY_RECORDER_RECENT_ROWS = STATE_HISTORY_MAX_TRANSITIONS * 4 + 1
STATE_KIND_NUMBER = "number"
STATE_KIND_STATE = "state"

_UNKNOWN = "unknown"
_UNAVAILABLE = "unavailable"
_RESERVED_PALETTE = (_UNKNOWN, _UNAVAILABLE)
_TEXT_DEVICE_CLASSES = frozenset({"date", "enum", "timestamp"})


@dataclass(frozen=True)
class StateHistoryRequest:
  """Validated textual-state history request limits."""

  hours: int
  max_transitions: int


class StateHistoryRequestError(ValueError):
  """A stable, publishable textual-state history request error."""

  def __init__(self, code: str) -> None:
    super().__init__(code)
    self.code = code


def parse_state_history_request(
  payload: Mapping[str, Any],
) -> StateHistoryRequest:
  """Validate the versioned textual-state history request contract."""
  version = _parse_integer(payload.get("version"))
  if version != STATE_HISTORY_VERSION:
    raise StateHistoryRequestError("unsupported_version")

  hours_value = payload.get("hours")
  hours = (
    STATE_HISTORY_DEFAULT_HOURS
    if hours_value is None
    else _parse_integer(hours_value)
  )
  if hours not in STATE_HISTORY_ALLOWED_HOURS:
    raise StateHistoryRequestError("invalid_hours")

  max_value = payload.get("max_transitions")
  max_transitions = (
    STATE_HISTORY_DEFAULT_MAX_TRANSITIONS
    if max_value is None
    else _parse_integer(max_value)
  )
  if (
    max_transitions is None
    or max_transitions < STATE_HISTORY_MIN_TRANSITIONS
    or max_transitions > STATE_HISTORY_MAX_TRANSITIONS
  ):
    raise StateHistoryRequestError("invalid_max_transitions")

  return StateHistoryRequest(hours=hours, max_transitions=max_transitions)


def build_state_history_error(entity_id: Any, code: str) -> dict[str, Any]:
  """Build a compact error on the versioned history response channel."""
  return {
    "version": STATE_HISTORY_VERSION,
    "kind": STATE_HISTORY_KIND,
    "entity_id": str(entity_id or "").strip(),
    "error": code,
  }


def sensor_state_kind(state: Any) -> str:
  """Classify a Home Assistant sensor as numeric or discrete-state.

  Home Assistant's sensor metadata is authoritative. A strict finite-number
  fallback keeps older custom sensors without state metadata compatible while
  ensuring date-like or otherwise partly numeric strings remain textual.
  """
  attributes = _state_attributes(state)
  device_class = _enum_text(attributes.get("device_class")).lower()
  options = attributes.get("options")
  if device_class in _TEXT_DEVICE_CLASSES or (
    isinstance(options, (list, tuple, set, frozenset)) and bool(options)
  ):
    return STATE_KIND_STATE

  state_class = _enum_text(attributes.get("state_class"))
  unit = attributes.get("unit_of_measurement")
  if state_class or (isinstance(unit, str) and bool(unit.strip())):
    return STATE_KIND_NUMBER

  value = _state_value(state)
  if _strict_finite_number(value) is not None:
    return STATE_KIND_NUMBER
  return STATE_KIND_STATE


def sensor_live_state_payload(state: Any) -> str:
  """Preserve textual states and normalize only proven numeric decimals."""
  raw_value = _state_value(state)
  raw = "" if raw_value is None else str(raw_value)
  if sensor_state_kind(state) == STATE_KIND_NUMBER:
    candidate = raw.replace(",", ".")
    if _strict_finite_number(candidate) is not None:
      return candidate
  return raw


def fetch_bounded_state_history(
  hass: Any,
  entity_id: str,
  start: datetime,
  end: datetime,
  *,
  state_changes_during_period: Optional[Callable[..., Any]],
  get_last_state_changes: Optional[Callable[..., Any]],
  recent_limit: int,
  page_size: int = STATE_HISTORY_RECORDER_PAGE_SIZE,
  max_changes: int = STATE_HISTORY_RECORDER_MAX_CHANGES,
  recent_rows: int = STATE_HISTORY_RECORDER_RECENT_ROWS,
) -> tuple[list[Any], bool, datetime]:
  """Read a complete period in bounded Recorder pages.

  The boolean indicates whether the scan reached ``end``. On an older Home
  Assistant Recorder API, or when the safety cap is reached, a bounded recent
  tail is returned as well so Activity remains useful while the unknown gap is
  represented explicitly by the response builder.
  """

  def fetch_recent(limit: int) -> list[Any]:
    if get_last_state_changes is None:
      return []
    recent_history = get_last_state_changes(hass, limit, entity_id)
    recent_states = (
      list(recent_history.get(entity_id, [])) if recent_history else []
    )
    return recent_states[-limit:]

  if state_changes_during_period is None:
    recent_states = fetch_recent(recent_limit)
    complete_until = start
    for state in recent_states:
      moment = _state_change_time(state)
      if moment is not None and moment > complete_until:
        complete_until = moment
    return recent_states, False, complete_until

  history_states: list[Any] = []
  cursor = start
  include_start_state = True
  scanned_changes = 0
  complete_until = start

  while scanned_changes < max_changes:
    page_limit = min(page_size, max_changes - scanned_changes)
    try:
      page = state_changes_during_period(
        hass,
        cursor,
        end,
        entity_id,
        include_start_time_state=include_start_state,
        no_attributes=True,
        limit=page_limit,
      )
    except TypeError:
      if not history_states:
        recent_states = fetch_recent(recent_limit)
        fallback_until = start
        for state in recent_states:
          moment = _state_change_time(state)
          if moment is not None and moment > fallback_until:
            fallback_until = moment
        return recent_states, False, fallback_until
      return history_states + fetch_recent(recent_rows), False, complete_until

    candidate_limit = page_limit + (1 if include_start_state else 0)
    candidates = (
      list(page.get(entity_id, []))[:candidate_limit] if page else []
    )
    page_changes: list[tuple[datetime, Any]] = []
    if include_start_state:
      for candidate in candidates:
        moment = _state_change_time(candidate)
        if moment is None or moment <= cursor:
          history_states.append(candidate)
          break
    for candidate in candidates:
      moment = _state_change_time(candidate)
      if moment is not None and moment > cursor:
        page_changes.append((moment, candidate))

    if not page_changes:
      return history_states, True, end
    page_changes.sort(key=lambda item: item[0])
    history_states.extend(candidate for _, candidate in page_changes)
    scanned_changes += len(page_changes)
    next_cursor = page_changes[-1][0]
    if next_cursor <= cursor:
      return history_states + fetch_recent(recent_rows), False, complete_until
    cursor = next_cursor
    complete_until = cursor
    include_start_state = False
    if len(page_changes) < page_limit:
      return history_states, True, end

  return history_states + fetch_recent(recent_rows), False, complete_until


def build_state_history_response(
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
  """Build a bounded categorical timeline and recent Activity list."""
  start = _unix_seconds(range_start)
  end = _unix_seconds(range_end)
  if start is None or end is None or end <= start:
    raise ValueError("range_end must be later than range_start")
  if (
    max_transitions < STATE_HISTORY_MIN_TRANSITIONS
    or max_transitions > STATE_HISTORY_MAX_TRANSITIONS
  ):
    raise ValueError("max_transitions is outside the protocol bounds")

  records: list[tuple[int, int, str]] = []
  for order, state in enumerate(history_states):
    timestamp = _state_timestamp(state)
    if timestamp is None or timestamp >= end:
      continue
    records.append((timestamp, order, _normalise_state(_state_value(state))))

  has_history_records = bool(records)
  current_timestamp = _state_timestamp(current_state)
  current_raw = (
    _bounded_current_state(_state_value(current_state))
    if current_state is not None
    else None
  )
  current_value = (
    _normalise_state(_state_value(current_state))
    if current_state is not None
    else None
  )
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
        current_value,
      )
    )

  records.sort(key=lambda item: (item[0], item[1]))
  records = _collapse_same_timestamp(records)

  initial_state = _UNKNOWN
  if not has_history_records and current_state is not None:
    initial_state = current_value

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
    current_continues_from_cutoff = (
      current_value is not None
      and current_timestamp is not None
      and current_timestamp <= complete_until
      and current_value == state_at_cutoff
    )
    if complete_until < end and not current_continues_from_cutoff:
      if state_at_cutoff != _UNKNOWN:
        timeline_transitions.append((complete_until, _UNKNOWN))
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
  palette, palette_complete = _build_palette(
    exact_segments, preferred_state=current_value
  )
  palette_index = {value: index for index, value in enumerate(palette)}
  timeline_codes = _timeline_codes_from_segments(
    exact_segments,
    start,
    end,
    STATE_HISTORY_TIMELINE_POINTS,
    palette_index,
  )
  segments = _bound_segments(exact_segments, start, max_transitions)
  activity = [
    {"timestamp": timestamp, "state": state}
    for timestamp, state in transitions[-max_transitions:]
  ]

  if current_state is None:
    current = None
    available = None
  else:
    current = current_raw
    available = current_value != _UNAVAILABLE

  return {
    "version": STATE_HISTORY_VERSION,
    "kind": STATE_HISTORY_KIND,
    "entity_id": entity_id,
    "hours": hours,
    "range_start": start,
    "range_end": end,
    "history_available": bool(history_available),
    "current": current,
    "available": available,
    "last_changed": current_timestamp,
    "timeline_points": STATE_HISTORY_TIMELINE_POINTS,
    "timeline_encoding": STATE_HISTORY_TIMELINE_ENCODING,
    "timeline_data": _encode_timeline_codes(timeline_codes),
    "timeline_complete": bool(history_available and history_complete),
    "palette": palette,
    "palette_complete": palette_complete,
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
  limit: int,
) -> list[dict[str, Any]]:
  if len(segments) <= limit:
    return segments
  recent = [dict(segment) for segment in segments[-(limit - 1):]]
  retained_start = int(recent[0]["start"])
  if recent[0]["state"] == _UNKNOWN:
    recent[0]["start"] = start
    return recent
  return [
    {"start": start, "end": retained_start, "state": _UNKNOWN},
    *recent,
  ]


def _build_palette(
  segments: list[dict[str, Any]],
  preferred_state: Optional[str] = None,
) -> tuple[list[str], bool]:
  unique: list[str] = []
  seen = set(_RESERVED_PALETTE)
  if preferred_state is not None:
    preferred = _normalise_state(preferred_state)
    if preferred not in seen:
      seen.add(preferred)
      unique.append(preferred)
  for segment in reversed(segments):
    state = _normalise_state(segment.get("state"))
    if state in seen:
      continue
    seen.add(state)
    unique.append(state)

  available_slots = STATE_HISTORY_MAX_PALETTE_ENTRIES - len(_RESERVED_PALETTE)
  palette_complete = len(unique) <= available_slots
  selected = unique[:available_slots]
  return [*_RESERVED_PALETTE, *selected], palette_complete


def _timeline_codes_from_segments(
  segments: list[dict[str, Any]],
  start: int,
  end: int,
  points: int,
  palette_index: Mapping[str, int],
) -> list[int]:
  unknown_code = int(palette_index[_UNKNOWN])
  codes = [unknown_code] * points
  coverage: list[dict[int, int]] = [{} for _ in range(points)]
  latest_code = [unknown_code] * points
  span = end - start
  for segment in segments:
    segment_start = max(start, int(segment["start"]))
    segment_end = min(end, int(segment["end"]))
    if segment_end <= segment_start:
      continue
    state = _normalise_state(segment.get("state"))
    code = int(palette_index.get(state, unknown_code))
    first = ((segment_start - start) * points) // span
    last = (((segment_end - start) * points) - 1) // span
    first = max(0, min(points - 1, first))
    last = max(first, min(points - 1, last))
    for index in range(first, last + 1):
      bucket_start = start + (span * index) // points
      bucket_end = start + (span * (index + 1)) // points
      overlap = max(
        0,
        min(segment_end, bucket_end) - max(segment_start, bucket_start),
      )
      if overlap <= 0:
        continue
      coverage[index][code] = coverage[index].get(code, 0) + overlap
      latest_code[index] = code
  for index, durations in enumerate(coverage):
    if not durations:
      continue
    greatest = max(durations.values())
    tied = {code for code, duration in durations.items() if duration == greatest}
    codes[index] = (
      latest_code[index]
      if latest_code[index] in tied
      else next(code for code, duration in durations.items() if duration == greatest)
    )
  return codes


def _encode_timeline_codes(codes: list[int]) -> str:
  packed = bytearray((len(codes) + 1) // 2)
  for index, code in enumerate(codes):
    shift = 4 if index % 2 == 0 else 0
    packed[index // 2] |= (code & 0x0F) << shift
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


def _state_change_time(state: Any) -> Optional[datetime]:
  if state is None:
    return None
  if isinstance(state, Mapping):
    value = state.get("last_updated")
    if value is None:
      value = state.get("last_changed")
  else:
    value = getattr(state, "last_updated", None)
    if value is None:
      value = getattr(state, "last_changed", None)
  return value if isinstance(value, datetime) else None


def _normalise_state(value: Any) -> str:
  state = str(value or "").strip()
  if not state:
    return _UNKNOWN
  lowered = state.lower()
  if lowered in _RESERVED_PALETTE:
    return lowered
  encoded = state.encode("utf-8")
  if len(encoded) <= STATE_HISTORY_MAX_STATE_BYTES:
    return state
  digest = hashlib.sha256(encoded).hexdigest()[:8]
  suffix = f"~{digest}"
  prefix_bytes = STATE_HISTORY_MAX_STATE_BYTES - len(suffix)
  prefix = encoded[:prefix_bytes].decode("utf-8", "ignore")
  return f"{prefix}{suffix}"


def _bounded_current_state(value: Any) -> str:
  """Keep the trimmed HA state readable while bounding the response field."""
  state = str(value or "").strip()
  encoded = state.encode("utf-8")
  if len(encoded) <= STATE_HISTORY_MAX_CURRENT_BYTES:
    return state
  return encoded[:STATE_HISTORY_MAX_CURRENT_BYTES].decode("utf-8", "ignore")


def _enum_text(value: Any) -> str:
  if hasattr(value, "value"):
    value = value.value
  return str(value or "").strip()


def _strict_finite_number(value: Any) -> Optional[float]:
  if isinstance(value, bool) or value is None:
    return None
  text = str(value).strip()
  if not text:
    return None
  try:
    parsed = float(text.replace(",", "."))
  except (TypeError, ValueError):
    return None
  return parsed if math.isfinite(parsed) else None


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
    try:
      return int(value)
    except (OverflowError, TypeError, ValueError):
      return None
  if isinstance(value, str):
    text = value.strip()
    if not text:
      return None
    try:
      return int(float(text))
    except (TypeError, ValueError, OverflowError):
      try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
      except ValueError:
        return None
      return _unix_seconds(parsed)
  return None


def _parse_integer(value: Any) -> Optional[int]:
  if isinstance(value, bool) or value is None:
    return None
  if isinstance(value, int):
    return value
  if isinstance(value, float):
    return int(value) if value.is_integer() else None
  if isinstance(value, str):
    text = value.strip()
    if not text or not text.lstrip("+-").isdigit():
      return None
    try:
      return int(text)
    except ValueError:
      return None
  return None
