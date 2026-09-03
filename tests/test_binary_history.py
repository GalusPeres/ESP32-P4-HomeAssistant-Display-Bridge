"""Dependency-free tests for the versioned binary history protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from typing import Any
import unittest


def _load_binary_history_module():
  path = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tab5_lvgl"
    / "binary_history.py"
  )
  spec = importlib.util.spec_from_file_location("_hometiles_binary_history", path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


BINARY_HISTORY = _load_binary_history_module()
UTC = timezone.utc


@dataclass
class FakeState:
  state: str
  last_changed: datetime | None
  attributes: dict[str, Any] = field(default_factory=dict)
  last_updated: datetime | None = None


class BinaryHistoryTest(unittest.TestCase):
  def test_request_contract_has_strict_version_ranges_and_defaults(self) -> None:
    request = BINARY_HISTORY.parse_binary_history_request({"version": 1})
    self.assertEqual(request.hours, 24)
    self.assertEqual(request.max_transitions, 48)

    request = BINARY_HISTORY.parse_binary_history_request(
      {
        "version": "1",
        "hours": 168,
        "max_transitions": 96,
      }
    )
    self.assertEqual(request.hours, 168)
    self.assertEqual(request.max_transitions, 96)

    invalid = (
      ({}, "unsupported_version"),
      ({"version": 2}, "unsupported_version"),
      ({"version": True}, "unsupported_version"),
      ({"version": 1, "hours": 48}, "invalid_hours"),
      ({"version": 1, "hours": True}, "invalid_hours"),
      ({"version": 1, "max_transitions": 0}, "invalid_max_transitions"),
      ({"version": 1, "max_transitions": 1}, "invalid_max_transitions"),
      ({"version": 1, "max_transitions": 97}, "invalid_max_transitions"),
      ({"version": 1, "max_transitions": 1.5}, "invalid_max_transitions"),
    )
    for payload, code in invalid:
      with self.subTest(payload=payload):
        with self.assertRaises(BINARY_HISTORY.BinaryHistoryRequestError) as ctx:
          BINARY_HISTORY.parse_binary_history_request(payload)
        self.assertEqual(ctx.exception.code, code)

  def test_response_is_sorted_contiguous_and_preserves_special_states(self) -> None:
    start = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    end = start + timedelta(hours=24)
    history = [
      FakeState("unknown", start + timedelta(seconds=60)),
      FakeState("off", start - timedelta(minutes=5)),
      FakeState("unavailable", start + timedelta(seconds=40)),
      FakeState("on", start + timedelta(seconds=20)),
    ]
    current = FakeState(
      "off",
      start + timedelta(seconds=80),
      {"device_class": "Occupancy"},
    )

    response = BINARY_HISTORY.build_binary_history_response(
      "binary_sensor.office",
      history,
      current,
      start,
      end,
      hours=24,
      max_transitions=10,
    )

    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    self.assertEqual(
      {key: response[key] for key in (
        "version",
        "kind",
        "entity_id",
        "hours",
        "range_start",
        "range_end",
        "history_available",
        "current",
        "available",
        "device_class",
        "last_changed",
      )},
      {
        "version": 1,
        "kind": "binary",
        "entity_id": "binary_sensor.office",
        "hours": 24,
        "range_start": start_ts,
        "range_end": end_ts,
        "history_available": True,
        "current": "off",
        "available": True,
        "device_class": "occupancy",
        "last_changed": start_ts + 80,
      },
    )
    self.assertEqual(
      response["segments"],
      [
        {"start": start_ts, "end": start_ts + 20, "state": "off"},
        {"start": start_ts + 20, "end": start_ts + 40, "state": "on"},
        {
          "start": start_ts + 40,
          "end": start_ts + 60,
          "state": "unavailable",
        },
        {
          "start": start_ts + 60,
          "end": start_ts + 80,
          "state": "unknown",
        },
        {"start": start_ts + 80, "end": end_ts, "state": "off"},
      ],
    )
    self.assertEqual(
      response["activity"],
      [
        {"timestamp": start_ts + 20, "state": "on"},
        {"timestamp": start_ts + 40, "state": "unavailable"},
        {"timestamp": start_ts + 60, "state": "unknown"},
        {"timestamp": start_ts + 80, "state": "off"},
      ],
    )

  def test_overflow_marks_omitted_prefix_unknown_and_keeps_recent_events(self) -> None:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(hours=168)
    states = [
      FakeState("off", start - timedelta(seconds=1)),
      FakeState("on", start + timedelta(seconds=10)),
      FakeState("off", start + timedelta(seconds=20)),
      FakeState("on", start + timedelta(seconds=30)),
      FakeState("unavailable", start + timedelta(seconds=40)),
    ]
    current = FakeState("off", start + timedelta(seconds=50))

    response = BINARY_HISTORY.build_binary_history_response(
      "binary_sensor.door",
      states,
      current,
      start,
      end,
      hours=168,
      max_transitions=3,
    )

    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    self.assertEqual(
      response["segments"],
      [
        {
          "start": start_ts,
          "end": start_ts + 40,
          "state": "unknown",
        },
        {
          "start": start_ts + 40,
          "end": start_ts + 50,
          "state": "unavailable",
        },
        {"start": start_ts + 50, "end": end_ts, "state": "off"},
      ],
    )
    self.assertEqual(
      response["activity"],
      [
        {"timestamp": start_ts + 30, "state": "on"},
        {"timestamp": start_ts + 40, "state": "unavailable"},
        {"timestamp": start_ts + 50, "state": "off"},
      ],
    )
    self.assertLessEqual(len(response["segments"]), 3)
    self.assertLessEqual(len(response["activity"]), 3)
    self.assertEqual(response["segments"][0]["start"], start_ts)
    self.assertEqual(response["segments"][-1]["end"], end_ts)

  def test_minimum_limit_keeps_a_known_recent_segment(self) -> None:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(hours=24)
    response = BINARY_HISTORY.build_binary_history_response(
      "binary_sensor.motion",
      [
        FakeState("off", start - timedelta(seconds=1)),
        FakeState("on", start + timedelta(seconds=10)),
      ],
      FakeState("off", start + timedelta(seconds=20)),
      start,
      end,
      hours=24,
      max_transitions=2,
    )

    self.assertEqual(
      response["segments"],
      [
        {
          "start": int(start.timestamp()),
          "end": int(start.timestamp()) + 20,
          "state": "unknown",
        },
        {
          "start": int(start.timestamp()) + 20,
          "end": int(end.timestamp()),
          "state": "off",
        },
      ],
    )
    self.assertEqual(
      response["activity"],
      [
        {"timestamp": int(start.timestamp()) + 10, "state": "on"},
        {"timestamp": int(start.timestamp()) + 20, "state": "off"},
      ],
    )

    with self.assertRaises(ValueError):
      BINARY_HISTORY.build_binary_history_response(
        "binary_sensor.motion",
        [],
        FakeState("off", start),
        start,
        end,
        hours=24,
        max_transitions=1,
      )

  def test_empty_history_uses_known_current_state_without_fake_activity(self) -> None:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(hours=24)
    current = FakeState(
      "on",
      start + timedelta(hours=12),
      {"device_class": "motion"},
    )

    for history_available in (True, False):
      with self.subTest(history_available=history_available):
        response = BINARY_HISTORY.build_binary_history_response(
          "binary_sensor.motion",
          [],
          current,
          start,
          end,
          hours=24,
          max_transitions=48,
          history_available=history_available,
        )
        self.assertIs(response["history_available"], history_available)
        self.assertEqual(response["activity"], [])
        self.assertEqual(
          response["segments"],
          [{
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "state": "on",
          }],
        )

  def test_same_second_updates_collapse_and_missing_entity_is_absent(self) -> None:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(hours=24)
    timestamp = start + timedelta(seconds=10, microseconds=100)
    response = BINARY_HISTORY.build_binary_history_response(
      "binary_sensor.test",
      [
        FakeState("off", start - timedelta(seconds=1)),
        FakeState("on", timestamp),
        FakeState("unavailable", timestamp + timedelta(microseconds=200)),
      ],
      None,
      start,
      end,
      hours=24,
      max_transitions=48,
    )

    transition_ts = int(timestamp.timestamp())
    self.assertIsNone(response["current"])
    self.assertIsNone(response["available"])
    self.assertIsNone(response["device_class"])
    self.assertIsNone(response["last_changed"])
    self.assertEqual(
      response["activity"],
      [{"timestamp": transition_ts, "state": "unavailable"}],
    )

  def test_current_unknown_and_unavailable_remain_distinct_from_absent(self) -> None:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(hours=24)
    cases = (
      (FakeState("unknown", start), "unknown", True, int(start.timestamp())),
      (FakeState("unavailable", start), "unavailable", False, int(start.timestamp())),
      (None, None, None, None),
    )

    for current_state, expected_current, expected_available, expected_changed in cases:
      with self.subTest(current_state=current_state):
        response = BINARY_HISTORY.build_binary_history_response(
          "binary_sensor.test",
          [],
          current_state,
          start,
          end,
          hours=24,
          max_transitions=48,
        )
        self.assertEqual(response["current"], expected_current)
        self.assertIs(response["available"], expected_available)
        self.assertEqual(response["last_changed"], expected_changed)

  def test_compact_error_uses_binary_response_envelope(self) -> None:
    self.assertEqual(
      BINARY_HISTORY.build_binary_history_error(
        " binary_sensor.office ", "invalid_hours"
      ),
      {
        "version": 1,
        "kind": "binary",
        "entity_id": "binary_sensor.office",
        "error": "invalid_hours",
      },
    )


if __name__ == "__main__":
  unittest.main()
