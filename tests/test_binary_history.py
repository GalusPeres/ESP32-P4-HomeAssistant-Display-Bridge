"""Dependency-free tests for the versioned binary history protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import importlib.util
import json
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


def decode_timeline(response: dict[str, Any]) -> list[int]:
  packed = bytes.fromhex(response["timeline_data"])
  points = response["timeline_points"]
  decoded: list[int] = []
  for value in packed:
    decoded.extend((
      (value >> 6) & 0x03,
      (value >> 4) & 0x03,
      (value >> 2) & 0x03,
      value & 0x03,
    ))
  return decoded[:points]


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
    self.assertEqual(response["timeline_points"], 768)
    self.assertEqual(response["timeline_encoding"], "2bit-hex")
    self.assertTrue(response["timeline_complete"])
    self.assertEqual(len(response["timeline_data"]), 384)

  def test_overflow_keeps_full_range_timeline_and_recent_events(self) -> None:
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
    timeline = decode_timeline(response)
    self.assertEqual(len(timeline), 768)
    self.assertEqual(timeline[0], 1)
    self.assertEqual(timeline[-1], 0)
    self.assertNotEqual(timeline[:585], [2] * 585)

  def test_additive_timeline_preserves_bounded_segments_and_activity(self) -> None:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(hours=24)
    states = [FakeState("off", start - timedelta(seconds=1))]
    states.extend(
      FakeState(
        "on" if index % 2 else "off",
        start + timedelta(minutes=index),
      )
      for index in range(1, 151)
    )

    response = BINARY_HISTORY.build_binary_history_response(
      "binary_sensor.compatibility",
      states,
      states[-1],
      start,
      end,
      hours=24,
      max_transitions=12,
    )

    self.assertIn("timeline_data", response)
    self.assertIn("segments", response)
    self.assertIn("activity", response)
    self.assertLessEqual(len(response["segments"]), 12)
    self.assertLessEqual(len(response["activity"]), 12)
    self.assertTrue(
      all(set(segment) == {"start", "end", "state"} for segment in response["segments"])
    )
    self.assertTrue(
      all(set(item) == {"timestamp", "state"} for item in response["activity"])
    )

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

    self.assertLessEqual(len(response["segments"]), 2)
    self.assertEqual(response["segments"][0]["start"], int(start.timestamp()))
    self.assertEqual(response["segments"][-1]["end"], int(end.timestamp()))
    self.assertEqual(decode_timeline(response)[0], 1)
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

  def test_busy_week_is_compact_and_preserves_early_and_late_activity(self) -> None:
    start = datetime(2026, 8, 27, tzinfo=UTC)
    end = start + timedelta(hours=168)
    history = [FakeState("off", start - timedelta(seconds=1))]
    for index in range(1, 601):
      history.append(
        FakeState(
          "on" if index % 2 else "off",
          start + timedelta(seconds=(index * 168 * 3600) // 602),
        )
      )
    current = FakeState("off", end - timedelta(minutes=1))

    response = BINARY_HISTORY.build_binary_history_response(
      "binary_sensor.busy_presence",
      history,
      current,
      start,
      end,
      hours=168,
      max_transitions=96,
    )

    timeline = decode_timeline(response)
    self.assertEqual(len(timeline), 768)
    self.assertIn(1, timeline[:256])
    self.assertIn(1, timeline[-256:])
    self.assertIn(0, timeline)
    self.assertEqual(len(response["activity"]), 96)
    self.assertLessEqual(len(response["segments"]), 96)
    self.assertLess(
      len(json.dumps(response, separators=(",", ":")).encode("utf-8")),
      32768,
    )

  def test_repeated_identical_rows_do_not_change_timeline_or_activity(self) -> None:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(hours=24)
    compact = [
      FakeState("off", start - timedelta(seconds=1)),
      FakeState("on", start + timedelta(hours=1)),
      FakeState("off", start + timedelta(hours=2)),
    ]
    noisy = [compact[0], compact[1]] + [
      FakeState("on", start + timedelta(hours=1, seconds=index))
      for index in range(1, 2001)
    ] + [compact[2]]
    current = FakeState("off", start + timedelta(hours=2))

    clean_response = BINARY_HISTORY.build_binary_history_response(
      "binary_sensor.test", compact, current, start, end, 24, 96
    )
    noisy_response = BINARY_HISTORY.build_binary_history_response(
      "binary_sensor.test", noisy, current, start, end, 24, 96
    )

    self.assertEqual(
      noisy_response["timeline_data"], clean_response["timeline_data"]
    )
    self.assertEqual(noisy_response["activity"], clean_response["activity"])

  def test_incomplete_scan_marks_gap_unknown_explicitly(self) -> None:
    start = datetime(2026, 9, 3, tzinfo=UTC)
    end = start + timedelta(hours=24)
    cutoff = start + timedelta(hours=2)
    response = BINARY_HISTORY.build_binary_history_response(
      "binary_sensor.test",
      [
        FakeState("off", start - timedelta(seconds=1)),
        FakeState("on", start + timedelta(hours=1)),
      ],
      FakeState("off", start + timedelta(hours=20)),
      start,
      end,
      24,
      96,
      history_complete=False,
      history_complete_until=cutoff,
    )

    timeline = decode_timeline(response)
    self.assertFalse(response["timeline_complete"])
    self.assertIn(1, timeline[:64])
    self.assertEqual(timeline[384], 2)
    self.assertEqual(timeline[-1], 0)

  def test_safety_cap_keeps_recent_activity_tail(self) -> None:
    start = datetime(2026, 8, 27, tzinfo=UTC)
    end = start + timedelta(hours=168)
    cutoff = start + timedelta(hours=84)
    prefix = [FakeState("off", start - timedelta(seconds=1))]
    for index in range(1, 8193):
      prefix.append(
        FakeState(
          "on" if index % 2 else "off",
          start + timedelta(seconds=(index * 84 * 3600) // 8194),
        )
      )
    tail_start = end - timedelta(hours=2)
    tail = [FakeState("off", tail_start)] + [
      FakeState(
        "on" if index % 2 else "off",
        tail_start + timedelta(minutes=30, seconds=index * 20),
      )
      for index in range(1, 201)
    ]
    response = BINARY_HISTORY.build_binary_history_response(
      "binary_sensor.busy_presence",
      prefix + tail,
      tail[-1],
      start,
      end,
      168,
      96,
      history_complete=False,
      history_complete_until=cutoff,
    )

    self.assertFalse(response["timeline_complete"])
    self.assertEqual(len(response["activity"]), 96)
    self.assertGreaterEqual(
      response["activity"][0]["timestamp"],
      int(tail_start.timestamp()),
    )
    timeline = decode_timeline(response)
    self.assertEqual(timeline[500], 2)
    tail_anchor_index = int(
      ((tail_start + timedelta(minutes=10) - start).total_seconds() * 768)
      // (168 * 3600)
    )
    self.assertEqual(timeline[tail_anchor_index], 0)
    self.assertIn(1, timeline[-16:])

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
