"""Dependency-free regressions for Bridge runtime sensor feedback."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys
import unittest


def _load_sensor_selection_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "tab5_lvgl"
        / "sensor_selection.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_hometiles_sensor_selection", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


SENSOR_SELECTION = _load_sensor_selection_module()


class SensorSelectionTest(unittest.TestCase):
    def _runtime_ids(
        self,
        *,
        labels=("Waveshare Touch LCD 4.3",),
        owned=(),
        announced=(),
    ) -> set[str]:
        return SENSOR_SELECTION.runtime_sensor_entity_id_candidates(
            labels, owned, announced, _slugify
        )

    def test_issue_5_roundtrip_removes_internal_aliases(self) -> None:
        feedback = [
            "sensor.waveshare_touch_lcd_4_3_batterie_soc",
            "sensor.waveshare_touch_lcd_4_3_externe_temperatur",
            "sensor.tab5_external_temperature",
            "sensor.tab5_internal_battery_soc",
            "sensor.outdoor_temperature",
        ]

        self.assertEqual(
            SENSOR_SELECTION.filter_runtime_sensor_entities(
                feedback, self._runtime_ids()
            ),
            ["sensor.outdoor_temperature"],
        )

    def test_preserves_legitimate_similar_and_unknown_sensors(self) -> None:
        configured = [
            "sensor.phone_battery_soc",
            "sensor.outdoor_temperature",
            "sensor.future_sensor_not_registered_yet",
            "sensor.phone_battery_soc",
        ]

        self.assertEqual(
            SENSOR_SELECTION.filter_runtime_sensor_entities(
                configured, self._runtime_ids()
            ),
            [
                "sensor.phone_battery_soc",
                "sensor.outdoor_temperature",
                "sensor.future_sensor_not_registered_yet",
            ],
        )

    def test_filters_user_renamed_and_announced_runtime_sensors(self) -> None:
        runtime_ids = self._runtime_ids(
            owned=("sensor.my_panel_power",),
            announced=("sensor.panel_case_temperature",),
        )

        self.assertEqual(
            SENSOR_SELECTION.filter_runtime_sensor_entities(
                [
                    "sensor.my_panel_power",
                    "sensor.panel_case_temperature",
                    "sensor.room_temperature",
                ],
                runtime_ids,
            ),
            ["sensor.room_temperature"],
        )

    def test_filters_name_only_internal_aliases_from_older_registries(self) -> None:
        self.assertEqual(
            SENSOR_SELECTION.filter_runtime_sensor_entities(
                [
                    "sensor.batterie_soc",
                    "sensor.externe_temperatur",
                    "sensor.room_temperature",
                ],
                self._runtime_ids(),
            ),
            ["sensor.room_temperature"],
        )

    def test_cleanup_is_idempotent(self) -> None:
        runtime_ids = self._runtime_ids()
        first = SENSOR_SELECTION.filter_runtime_sensor_entities(
            [
                "sensor.waveshare_touch_lcd_4_3_batterie_soc",
                "sensor.room_temperature",
            ],
            runtime_ids,
        )
        second = SENSOR_SELECTION.filter_runtime_sensor_entities(first, runtime_ids)

        self.assertEqual(first, ["sensor.room_temperature"])
        self.assertEqual(second, first)

    def test_cleans_data_and_options_without_mutating_other_values(self) -> None:
        stale = "sensor.waveshare_touch_lcd_4_3_batterie_soc"
        data = {
            "sensors": [stale, "sensor.room_temperature"],
            "lights": ["light.desk"],
        }
        options = {
            "sensors": [stale],
            "custom": "keep",
        }

        cleaned = SENSOR_SELECTION.clean_stored_sensor_selections(
            data, options, "sensors", self._runtime_ids()
        )
        cleaned_data, cleaned_options, data_changed, options_changed, removed = (
            cleaned
        )

        self.assertEqual(cleaned_data["sensors"], ["sensor.room_temperature"])
        self.assertEqual(cleaned_data["lights"], ["light.desk"])
        self.assertEqual(cleaned_options, {"sensors": [], "custom": "keep"})
        self.assertTrue(data_changed)
        self.assertTrue(options_changed)
        self.assertEqual(removed, 2)
        self.assertEqual(data["sensors"][0], stale)
        self.assertEqual(options["sensors"], [stale])

        second = SENSOR_SELECTION.clean_stored_sensor_selections(
            cleaned_data, cleaned_options, "sensors", self._runtime_ids()
        )
        self.assertFalse(second[2])
        self.assertFalse(second[3])
        self.assertEqual(second[4], 0)

    def test_feedback_only_initializes_a_missing_selection(self) -> None:
        should_import = SENSOR_SELECTION.should_import_feedback_selection
        incoming = ["sensor.room_temperature"]

        self.assertTrue(should_import({}, {}, "sensors", incoming))
        self.assertFalse(should_import({"sensors": []}, {}, "sensors", incoming))
        self.assertFalse(should_import({}, {"sensors": []}, "sensors", incoming))
        self.assertFalse(should_import({}, {}, "sensors", []))


if __name__ == "__main__":
    unittest.main()
