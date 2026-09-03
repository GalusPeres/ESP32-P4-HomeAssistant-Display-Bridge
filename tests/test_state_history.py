"""Dependency-free tests for textual sensor-state history."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any
import unittest


def _load_state_history_module():
  path = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tab5_lvgl"
    / "state_history.py"
  )
  spec = importlib.util.spec_from_file_location("_hometiles_state_history", path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


STATE_HISTORY = _load_state_history_module()
UTC = timezone.utc


@dataclass
class FakeState:
  state: str
  last_changed: datetime | None
  attributes: dict[str, Any] = field(default_factory=dict)
  last_updated: datetime | None = None


def decode_timeline(response: dict[str, Any]) -> list[int]:
  return [int(character, 16) for character in response["timeline_data"]][
    : response["timeline_points"]
  ]


class StateHistoryTest(unittest.TestCase):
  def test_request_contract_has_strict_version_ranges_and_defaults(self) -> None:
    request = STATE_HISTORY.parse_state_history_request({"version": 1})
    self.assertEqual(request.hours, 24)
    self.assertEqual(request.max_transitions, 48)

    request = STATE_HISTORY.parse_state_history_request(
      {"version": "1", "hours": 168, "max_transitions": 96}
    )
    self.assertEqual(request.hours, 168)
    self.assertEqual(request.max_transitions, 96)

    invalid = (
      ({}, "unsupported_version"),
      ({"version": 2}, "unsupported_version"),
      ({"version": True}, "unsupported_version"),
      ({"version": 1, "hours": 48}, "invalid_hours"),
      ({"version": 1, "hours": True}, "invalid_hours"),
      ({"version": 1, "max_transitions": 1}, "invalid_max_transitions"),
      ({"version": 1, "max_transitions": 97}, "invalid_max_transitions"),
      ({"version": 1, "max_transitions": 1.5}, "invalid_max_transitions"),
    )
    for payload, code in invalid:
      with self.subTest(payload=payload):
        with self.assertRaises(STATE_HISTORY.StateHistoryRequestError) as ctx:
          STATE_HISTORY.parse_state_history_request(payload)
        self.assertEqual(ctx.exception.code, code)

  def test_sensor_kind_prefers_ha_metadata_then_strict_finite_number(self) -> None:
    cases = (
      (FakeState("unavailable", None, {"state_class": "measurement"}), "number"),
      (FakeState("unknown", None, {"unit_of_measurement": "°C"}), "number"),
      (FakeState("1", None, {"device_class": "enum", "options": ["1"]}), "state"),
      (FakeState("2026-09-04", None, {"device_class": "date"}), "state"),
      (FakeState("2026-09-04T12:00:00Z", None, {"device_class": "timestamp"}), "state"),
      (FakeState("-1.2e3", None), "number"),
      (FakeState("12,3", None), "number"),
      (FakeState("12abc", None), "state"),
      (FakeState("nan", None), "state"),
      (FakeState("inf", None), "state"),
      (FakeState("Restmüll, morgen", None), "state"),
    )
    for state, expected in cases:
      with self.subTest(state=state.state, attributes=state.attributes):
        self.assertEqual(STATE_HISTORY.sensor_state_kind(state), expected)

  def test_live_payload_preserves_text_and_normalizes_only_numeric_decimal(self) -> None:
    self.assertEqual(
      STATE_HISTORY.sensor_live_state_payload(FakeState("Restmüll, morgen", None)),
      "Restmüll, morgen",
    )
    self.assertEqual(
      STATE_HISTORY.sensor_live_state_payload(
        FakeState("1,2", None, {"device_class": "enum", "options": ["1,2"]})
      ),
      "1,2",
    )
    self.assertEqual(
      STATE_HISTORY.sensor_live_state_payload(FakeState("12,3", None)),
      "12.3",
    )

  def test_response_preserves_text_and_deduplicates_attribute_rows(self) -> None:
    start = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    end = start + timedelta(hours=24)
    paper_changed = start + timedelta(hours=2)
    history = [
      FakeState("Restmüll, morgen_v2", start - timedelta(minutes=5)),
      FakeState("Papier", paper_changed, last_updated=paper_changed),
      FakeState(
        "Papier",
        paper_changed,
        {"friendly_name": "Updated attributes only"},
        paper_changed + timedelta(minutes=30),
      ),
      FakeState("Gelber Sack", start + timedelta(hours=5)),
    ]
    current = FakeState("Papier", start + timedelta(hours=8))

    response = STATE_HISTORY.build_state_history_response(
      "sensor.next_collection_waste",
      history,
      current,
      start,
      end,
      hours=24,
      max_transitions=10,
    )

    start_ts = int(start.timestamp())
    self.assertEqual(response["kind"], "state")
    self.assertEqual(response["current"], "Papier")
    self.assertEqual(response["timeline_encoding"], "palette4-hex")
    self.assertEqual(response["timeline_points"], 768)
    self.assertEqual(len(response["timeline_data"]), 768)
    self.assertLessEqual(len(response["palette"]), 16)
    self.assertTrue(response["palette_complete"])
    self.assertEqual(response["palette"][:2], ["unknown", "unavailable"])
    self.assertEqual(
      response["activity"],
      [
        {"timestamp": start_ts + 2 * 3600, "state": "Papier"},
        {"timestamp": start_ts + 5 * 3600, "state": "Gelber Sack"},
        {"timestamp": start_ts + 8 * 3600, "state": "Papier"},
      ],
    )
    self.assertEqual(response["segments"][0]["state"], "Restmüll, morgen_v2")
    timeline = decode_timeline(response)
    self.assertTrue(all(0 <= code < len(response["palette"]) for code in timeline))
    self.assertEqual(timeline[0], response["palette"].index("Restmüll, morgen_v2"))
    self.assertEqual(timeline[-1], response["palette"].index("Papier"))

  def test_timeline_uses_longest_coverage_and_latest_state_for_a_tie(self) -> None:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(seconds=7680)
    response = STATE_HISTORY.build_state_history_response(
      "sensor.mode",
      [
        FakeState("A", start - timedelta(seconds=1)),
        FakeState("B", start + timedelta(seconds=5)),
        FakeState("C", start + timedelta(seconds=10)),
      ],
      FakeState("C", start + timedelta(seconds=10)),
      start,
      end,
      24,
      10,
    )
    timeline = decode_timeline(response)
    self.assertEqual(timeline[0], response["palette"].index("B"))
    self.assertEqual(timeline[1], response["palette"].index("C"))

  def test_busy_week_is_full_range_compact_and_activity_is_bounded(self) -> None:
    start = datetime(2026, 8, 27, tzinfo=UTC)
    end = start + timedelta(hours=168)
    values = ("Restmüll", "Papier", "Gelber Sack")
    history = [FakeState(values[0], start - timedelta(seconds=1))]
    for index in range(1, 601):
      history.append(
        FakeState(
          values[index % len(values)],
          start + timedelta(seconds=(index * 168 * 3600) // 602),
        )
      )
    current = FakeState(values[2], end - timedelta(minutes=1))

    response = STATE_HISTORY.build_state_history_response(
      "sensor.next_collection_waste",
      history,
      current,
      start,
      end,
      168,
      96,
    )
    timeline = decode_timeline(response)
    self.assertEqual(len(timeline), 768)
    self.assertNotEqual(timeline[:256], [0] * 256)
    self.assertNotEqual(timeline[-256:], [0] * 256)
    self.assertEqual(len(response["activity"]), 96)
    self.assertLessEqual(len(response["segments"]), 96)
    payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    self.assertLessEqual(
      len(payload.encode("utf-8")),
      STATE_HISTORY.STATE_HISTORY_MAX_RESPONSE_BYTES,
    )

  def test_palette_overflow_is_explicit_and_never_emits_invalid_codes(self) -> None:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(hours=24)
    history = [FakeState("state-0", start - timedelta(seconds=1))]
    for index in range(1, 40):
      history.append(FakeState(f"state-{index}", start + timedelta(minutes=index)))
    current = FakeState("latest-current", end + timedelta(seconds=1))
    response = STATE_HISTORY.build_state_history_response(
      "sensor.mode", history, current, start, end, 24, 96
    )

    self.assertFalse(response["palette_complete"])
    self.assertEqual(len(response["palette"]), 16)
    self.assertIn("latest-current", response["palette"])
    self.assertEqual(response["current"], "latest-current")
    self.assertIn(0, decode_timeline(response))
    self.assertTrue(
      all(0 <= code < len(response["palette"]) for code in decode_timeline(response))
    )

  def test_long_utf8_states_are_bounded_without_prefix_collisions(self) -> None:
    prefix = "🗑️" * 20
    first = STATE_HISTORY._normalise_state(prefix + "-A")
    second = STATE_HISTORY._normalise_state(prefix + "-B")
    self.assertNotEqual(first, second)
    self.assertLessEqual(
      len(first.encode("utf-8")), STATE_HISTORY.STATE_HISTORY_MAX_STATE_BYTES
    )
    self.assertLessEqual(
      len(second.encode("utf-8")), STATE_HISTORY.STATE_HISTORY_MAX_STATE_BYTES
    )

    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(hours=24)
    hostile_values = [
      (f'"\\🗑️-{index}-' * 20)
      for index in range(220)
    ]
    history = [FakeState(hostile_values[0], start - timedelta(seconds=1))]
    history.extend(
      FakeState(value, start + timedelta(minutes=index + 1))
      for index, value in enumerate(hostile_values[1:])
    )
    response = STATE_HISTORY.build_state_history_response(
      "sensor.long_state", history, history[-1], start, end, 24, 96
    )
    expected_current = hostile_values[-1].strip().encode("utf-8")[
      :STATE_HISTORY.STATE_HISTORY_MAX_CURRENT_BYTES
    ].decode("utf-8", "ignore")
    canonical_current = STATE_HISTORY._normalise_state(hostile_values[-1])
    self.assertEqual(response["current"], expected_current)
    self.assertNotEqual(response["current"], canonical_current)
    self.assertIn(canonical_current, response["palette"])
    self.assertNotIn(response["current"], response["palette"])
    self.assertEqual(response["activity"][-1]["state"], canonical_current)
    self.assertLessEqual(
      len(response["current"].encode("utf-8")),
      STATE_HISTORY.STATE_HISTORY_MAX_CURRENT_BYTES,
    )
    payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    self.assertEqual(json.loads(payload)["kind"], "state")
    self.assertLessEqual(
      len(payload.encode("utf-8")),
      STATE_HISTORY.STATE_HISTORY_MAX_RESPONSE_BYTES,
    )

  def test_missing_unknown_unavailable_and_incomplete_scan_are_distinct(self) -> None:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(hours=24)
    missing = STATE_HISTORY.build_state_history_response(
      "sensor.test", [], None, start, end, 24, 10, history_available=False
    )
    self.assertIsNone(missing["current"])
    self.assertIsNone(missing["available"])
    self.assertFalse(missing["history_available"])
    self.assertFalse(missing["timeline_complete"])

    unavailable = STATE_HISTORY.build_state_history_response(
      "sensor.test",
      [FakeState("unknown", start - timedelta(seconds=1))],
      FakeState("unavailable", start + timedelta(hours=20)),
      start,
      end,
      24,
      10,
      history_complete=False,
      history_complete_until=start + timedelta(hours=2),
    )
    self.assertEqual(unavailable["current"], "unavailable")
    self.assertFalse(unavailable["available"])
    self.assertFalse(unavailable["timeline_complete"])
    timeline = decode_timeline(unavailable)
    self.assertEqual(timeline[384], unavailable["palette"].index("unknown"))
    self.assertEqual(timeline[-1], unavailable["palette"].index("unavailable"))

  def test_recorder_fetch_pages_to_end_and_has_bounded_fallbacks(self) -> None:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(hours=24)
    entity_id = "sensor.mode"
    anchor = FakeState("A", start - timedelta(seconds=1), last_updated=start - timedelta(seconds=1))
    changes = [
      FakeState(
        "A" if index % 2 else "B",
        start + timedelta(seconds=index),
        last_updated=start + timedelta(seconds=index),
      )
      for index in range(1, 601)
    ]
    calls: list[tuple[datetime, int, bool]] = []

    def period(_hass, cursor, _end, requested_entity, **kwargs):
      self.assertEqual(requested_entity, entity_id)
      calls.append((cursor, kwargs["limit"], kwargs["include_start_time_state"]))
      page = [state for state in changes if state.last_updated > cursor][
        : kwargs["limit"]
      ]
      if kwargs["include_start_time_state"]:
        page.insert(0, anchor)
      return {entity_id: page}

    states, complete, complete_until = STATE_HISTORY.fetch_bounded_state_history(
      object(),
      entity_id,
      start,
      end,
      state_changes_during_period=period,
      get_last_state_changes=None,
      recent_limit=97,
    )
    self.assertTrue(complete)
    self.assertEqual(complete_until, end)
    self.assertEqual(len(states), 601)
    self.assertEqual([call[1] for call in calls], [256, 256, 256])
    self.assertTrue(calls[0][2])
    self.assertFalse(calls[1][2])

    recent = changes[-120:]

    def unsupported_period(*_args, **_kwargs):
      raise TypeError("old Recorder signature")

    def get_last(_hass, limit, requested_entity):
      self.assertEqual(requested_entity, entity_id)
      return {entity_id: recent[-limit:]}

    fallback, complete, _ = STATE_HISTORY.fetch_bounded_state_history(
      object(),
      entity_id,
      start,
      end,
      state_changes_during_period=unsupported_period,
      get_last_state_changes=get_last,
      recent_limit=97,
    )
    self.assertFalse(complete)
    self.assertEqual(len(fallback), 97)

    capped, complete, complete_until = STATE_HISTORY.fetch_bounded_state_history(
      object(),
      entity_id,
      start,
      end,
      state_changes_during_period=period,
      get_last_state_changes=get_last,
      recent_limit=97,
      page_size=32,
      max_changes=32,
      recent_rows=20,
    )
    self.assertFalse(complete)
    self.assertEqual(len(capped), 53)
    self.assertEqual(complete_until, changes[31].last_updated)


if __name__ == "__main__":
  unittest.main()
