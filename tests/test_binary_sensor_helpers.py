"""Dependency-free tests for the HomeTiles binary-sensor contract."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import unittest


def _load_binary_sensor_helpers_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "tab5_lvgl"
        / "binary_sensor_helpers.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_hometiles_binary_sensor_helpers", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BINARY_SENSORS = _load_binary_sensor_helpers_module()


class BinarySensorHelpersTest(unittest.TestCase):
    def test_splits_legacy_binary_sensors_without_losing_order(self) -> None:
        binary_sensors, sensors = BINARY_SENSORS.split_binary_sensor_entities(
            [
                "sensor.temperature",
                "binary_sensor.desk_presence",
                "sensor.humidity",
                "binary_sensor.desk_presence",
                "binary_sensor.window",
            ]
        )

        self.assertEqual(
            binary_sensors,
            ["binary_sensor.desk_presence", "binary_sensor.window"],
        )
        self.assertEqual(sensors, ["sensor.temperature", "sensor.humidity"])

    def test_migrates_data_and_options_without_mutating_inputs(self) -> None:
        data = {
            "sensors": ["sensor.temperature", "binary_sensor.window"],
            "binary_sensors": ["binary_sensor.motion"],
            "lights": ["light.desk"],
        }
        options = {
            "sensors": ["binary_sensor.door", "sensor.humidity"],
            "custom": "keep",
        }

        migrated_data, migrated_options, changed = (
            BINARY_SENSORS.migrate_binary_sensor_config(data, options)
        )

        self.assertTrue(changed)
        self.assertEqual(migrated_data["sensors"], ["sensor.temperature"])
        self.assertEqual(
            migrated_data["binary_sensors"],
            ["binary_sensor.motion", "binary_sensor.window"],
        )
        self.assertEqual(migrated_options["sensors"], ["sensor.humidity"])
        self.assertEqual(
            migrated_options["binary_sensors"], ["binary_sensor.door"]
        )
        self.assertEqual(migrated_options["custom"], "keep")
        self.assertEqual(
            data["sensors"], ["sensor.temperature", "binary_sensor.window"]
        )
        self.assertNotIn("binary_sensors", options)

        second = BINARY_SENSORS.migrate_binary_sensor_config(
            migrated_data, migrated_options
        )
        self.assertFalse(second[2])
        self.assertEqual(second[0], migrated_data)
        self.assertEqual(second[1], migrated_options)

    def test_explicit_empty_binary_selection_stays_explicit(self) -> None:
        data, options, changed = BINARY_SENSORS.migrate_binary_sensor_config(
            {"sensors": ["sensor.temperature"], "binary_sensors": []},
            {"sensors": [], "binary_sensors": []},
        )

        self.assertFalse(changed)
        self.assertEqual(data["binary_sensors"], [])
        self.assertEqual(options["binary_sensors"], [])

    def test_legacy_options_removal_does_not_restore_data_binary_sensor(self) -> None:
        data, options, changed = BINARY_SENSORS.migrate_binary_sensor_config(
            {
                "sensors": ["sensor.temperature", "binary_sensor.old_window"],
            },
            {
                "sensors": ["sensor.temperature"],
            },
        )

        self.assertTrue(changed)
        self.assertEqual(data["binary_sensors"], ["binary_sensor.old_window"])
        self.assertEqual(options["binary_sensors"], [])
        merged = dict(data)
        merged.update(options)
        self.assertEqual(merged["binary_sensors"], [])

    def test_state_payload_preserves_binary_semantics_and_timestamp(self) -> None:
        payload = BINARY_SENSORS.build_binary_sensor_state_payload(
            "ON",
            {"device_class": "Occupancy"},
            datetime(2026, 9, 3, 8, 32, 50, tzinfo=timezone.utc),
            icon="mdi:home",
        )

        self.assertEqual(
            payload,
            {
                "state": "on",
                "available": True,
                "device_class": "occupancy",
                "last_changed": 1788424370,
                "icon": "mdi:home",
            },
        )

    def test_unknown_and_unavailable_remain_distinct(self) -> None:
        unknown = BINARY_SENSORS.build_binary_sensor_state_payload(
            "unknown", {}, None
        )
        unavailable = BINARY_SENSORS.build_binary_sensor_state_payload(
            "unavailable", {"device_class": "door"}, None
        )

        self.assertEqual(unknown["state"], "unknown")
        self.assertIs(unknown["available"], True)
        self.assertIsNone(unknown["device_class"])
        self.assertIsNone(unknown["last_changed"])
        self.assertEqual(unavailable["state"], "unavailable")
        self.assertIs(unavailable["available"], False)
        self.assertEqual(unavailable["device_class"], "door")

    def test_absent_state_uses_explicit_null_tombstone(self) -> None:
        absent = BINARY_SENSORS.build_binary_sensor_state_payload(
            None,
            {"device_class": "door"},
            datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            absent,
            {
                "state": None,
                "available": None,
                "device_class": None,
                "last_changed": None,
            },
        )

    def test_metadata_contains_initial_state_and_display_fields(self) -> None:
        entry = BINARY_SENSORS.build_binary_sensor_meta_entry(
            "binary_sensor.window",
            "off",
            {"device_class": "window"},
            datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc),
            name=" Kitchen Window ",
            icon=" mdi:window-closed ",
        )

        self.assertEqual(entry["entity_id"], "binary_sensor.window")
        self.assertEqual(entry["state"], "off")
        self.assertIs(entry["available"], True)
        self.assertEqual(entry["device_class"], "window")
        self.assertIsInstance(entry["last_changed"], int)
        self.assertEqual(entry["name"], "Kitchen Window")
        self.assertEqual(entry["icon"], "mdi:window-closed")

    def test_unexpected_binary_state_is_defensively_unknown(self) -> None:
        payload = BINARY_SENSORS.build_binary_sensor_state_payload(
            "detected", {"device_class": "motion"}, None
        )
        self.assertEqual(payload["state"], "unknown")
        self.assertIs(payload["available"], True)

    def test_device_class_is_bounded_for_embedded_consumers(self) -> None:
        payload = BINARY_SENSORS.build_binary_sensor_state_payload(
            "off", {"device_class": "x" * 200}, None
        )
        self.assertEqual(payload["device_class"], "x" * 64)

    def test_live_icon_is_trimmed_and_bounded_for_embedded_consumers(self) -> None:
        payload = BINARY_SENSORS.build_binary_sensor_state_payload(
            "off", {"device_class": "door"}, None, icon="  mdi:" + "x" * 100
        )
        self.assertEqual(payload["icon"], ("mdi:" + "x" * 100)[:39])


if __name__ == "__main__":
    unittest.main()
