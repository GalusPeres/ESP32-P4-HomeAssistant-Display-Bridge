# <img src="logo.png" width="34" alt="" align="top"> HomeTiles Bridge

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=GalusPeres&repository=HomeTiles-Bridge&category=integration)

Home Assistant custom integration for the [HomeTiles](https://github.com/GalusPeres/HomeTiles) project. Bridges Home Assistant entities, sensors, weather, energy data and more to ESP32-based LVGL displays via MQTT.

## About

This integration is the Home Assistant companion for the **HomeTiles** firmware. It handles:

- Pushing entity states, metadata and icons to the display in real time
- Numeric sensor graphs plus bounded binary and textual-state timelines for 24 hours or 7 days
- Weather forecasts (daily + hourly)
- Energy dashboard data (consumption, solar, grid, battery, gas, water)
- Light, switch, cover, climate, media player and scene control from the display
- Experimental camera popups with local, receiver-paced JPEG video transport
- Auto-discovery of integration-owned sensors and device-announced local I/O

**Firmware repository:** [HomeTiles](https://github.com/GalusPeres/HomeTiles)

**Documentation:** [galusperes.github.io/HomeTiles](https://galusperes.github.io/HomeTiles/) — full setup guide, [bridge configuration](https://galusperes.github.io/HomeTiles/bridge/), tile reference, and FAQ

## Installation

### Via HACS (Recommended)

1. Click the "Open in HACS" badge above (opens the custom repository dialog directly in your Home Assistant), or add it manually:
   - HACS > Integrations > three-dot menu (top right) > Custom repositories
   - Repository: `https://github.com/GalusPeres/HomeTiles-Bridge`
   - Category: Integration
   - Click "Add"

2. Install the integration:
   - HACS > Integrations > Search for "HomeTiles Bridge"
   - Click "Download"

3. Restart Home Assistant

4. Add the integration:
   - Settings > Devices & Services > Add Integration
   - Search for "HomeTiles Bridge"

### Manual Installation

1. Copy the `custom_components/tab5_lvgl` directory to your Home Assistant `custom_components` folder
2. Restart Home Assistant
3. Add the integration via Settings > Devices & Services

## Configuration

Detailed instructions: [bridge documentation](https://galusperes.github.io/HomeTiles/bridge/)

Configure via the Home Assistant UI:

- **Panel Settings** - MQTT base topic, HA prefix, device metadata
- **Entity Configuration** - Sensors, binary sensors, weather, lights, switches, covers, climate devices, media players, scenes
- **Energy Dashboard** - Electricity, gas and water from the HA Energy Dashboard

## MQTT Topics

The integration communicates with the display firmware via MQTT:

| Topic | Direction | Description |
|---|---|---|
| `base_topic/stat/connected` | Display > HA | Connection status |
| `tab5_lvgl/config/{id}/bridge` | Display > HA | Device announcement and local I/O discovery |
| `tab5_lvgl/config/{id}/bridge/apply` | HA > Display | Full configuration push |
| `tab5_lvgl/config/{id}/bridge/icons` | HA > Display | Lightweight icon updates |
| `tab5_lvgl/config/{id}/history/*` | Bidirectional | Numeric, binary-sensor and textual sensor-state history request/response |
| `tab5_lvgl/config/{id}/weather/*` | Bidirectional | Weather forecast request/response |
| `tab5_lvgl/config/{id}/energy/*` | Bidirectional | Energy data request/response |
| `base_topic/cmnd/light` | Display > HA | Light control commands |
| `base_topic/cmnd/switch` | Display > HA | Switch control commands |
| `base_topic/cmnd/media` | Display > HA | Media player commands |
| `base_topic/cmnd/climate` | Display > HA | Climate temperature and HVAC mode commands |
| `base_topic/cmnd/cover` | Display > HA | Cover position, tilt, open, close and stop commands |
| `base_topic/cmnd/scene` | Display > HA | Scene activation |
| `base_topic/cmnd/camera` | Display > HA | Open or close an experimental camera stream |
| `base_topic/stat/camera` | HA > Display | Camera stream endpoint, protocol and status |
| `base_topic/cmnd/display_brightness` | HA > Display | Set normal display brightness (1-100%) |
| `base_topic/stat/display_brightness` | Display > HA | Current normal display brightness (1-100%) |
| `base_topic/cmnd/screensaver_brightness` | HA > Display | Set screensaver brightness (1-100%) |
| `base_topic/stat/screensaver_brightness` | Display > HA | Current screensaver brightness (1-100%) |
| `base_topic/cmnd/io/{channel_id}` | HA > Display | Local relay command (`ON`/`OFF`, not retained) |
| `base_topic/stat/io/{channel_id}` | Display > HA | Retained local relay or temperature state |

### Binary sensor protocol

The retained bridge configuration contains dedicated `binary_sensors` and
`binary_sensor_meta` arrays. Each available metadata entry includes its current
`state`, `available`, `device_class`, `last_changed`, name and icon. Live state
uses `<ha_prefix>/binary_sensor/<object_id>/state` with this JSON contract:

```json
{"state":"on","available":true,"device_class":"occupancy","last_changed":1788424370,"icon":"mdi:home"}
```

The live `icon` is resolved by Home Assistant for the current state. Default
device-class icons therefore follow state changes, while an icon explicitly set
in Home Assistant remains fixed; an icon selected directly on the HomeTiles tile
still has highest priority. If a configured entity no longer exists, the bridge
publishes a retained JSON-null tombstone so an old `on` or `off` value cannot
remain visible.

Legacy configurations that stored `binary_sensor.*` IDs in `sensors` are
migrated automatically. The numeric history request remains unchanged. Binary
history uses a separate versioned mode and accepts only configured entities:

```json
{"version":1,"kind":"binary","entity_id":"binary_sensor.desk_presence","hours":24,"max_transitions":48}
```

`hours` is limited to `24` or `168`; `max_transitions` defaults to 48 and must
be between 2 and 96. The response contains Unix-second `range_start`,
`range_end` and `last_changed` values, the current state and device class, plus
chronologically sorted `segments` (`start`, `end`, `state`) and `activity`
(`timestamp`, `state`). Both arrays remain within the requested limit. An
additive `2bit-hex` timeline (`timeline_points`, `timeline_encoding`, and
`timeline_data`) is built from paged Recorder state changes across the complete
requested period, so a busy 7-day sensor is not reduced to only its newest 96
changes. `timeline_complete` reports whether the bounded Recorder scan reached
the end of the period. `unknown` and `unavailable` are preserved.
`history_available: false` distinguishes a Recorder failure from a successful
query with no transitions.

Backward compatibility is part of the history contract. A request without a
`kind` discriminator always uses the established numeric response shape,
regardless of the entity's newly advertised `state_kind`. Binary responses keep
their bounded `segments` and `activity` arrays; the compact timeline is additive.
Likewise, `state_kind` only extends `sensor_meta` and does not replace its
existing entity ID, unit, name, value or icon fields. Older firmware may omit the
new request fields and ignore the new response and metadata fields safely.

### Textual sensor-state protocol

Each `sensor_meta` entry now adds `state_kind`, either `number` or `state`.
Home Assistant sensor metadata is authoritative: numerical state classes and
units remain on the established graph path, while enum, date, timestamp and
other nonnumeric sensor states use the same History and Activity layout as a
Binary Sensor. Older firmware safely ignores this additive metadata field.

The retained live state topic remains a plain payload for backwards
compatibility. Text is preserved exactly, including commas, underscores and
Unicode; decimal-comma normalization is applied only when the sensor is proven
numeric. History labels are limited to 32 UTF-8 bytes for the embedded payload;
longer values receive a stable hash suffix so different states are not silently
merged. The response's `current` value remains the trimmed HA state for display
and live-state comparison and is independently bounded to 255 UTF-8 bytes.
Textual history uses a separate request mode and accepts only a
configured `sensor.*` entity:

```json
{"version":1,"kind":"state","entity_id":"sensor.next_collection_waste","hours":168,"max_transitions":96}
```

The response provides the same bounded range, current-state, segment and
Activity fields as Binary Sensor history. Its 768-point timeline uses
`timeline_encoding: "palette4-hex"`: each hexadecimal digit indexes one of at
most 16 canonical, bounded state strings in `palette`. `unknown` and
`unavailable` occupy the first two entries. `palette_complete: false` explicitly
reports that more unique values existed than the embedded palette can represent;
timeline cells for omitted values deliberately use the safe `unknown` color.
The separate
`timeline_complete` independently reports whether the bounded Recorder scan
covered the full requested 24 hours or 7 days. Consecutive identical states are
collapsed, so attribute-only updates do not create false Activity rows.

Firmware may advertise local relays and temperature inputs in its device
announcement. IDs must be unique per panel and stay stable across firmware
updates. Omitting `local_io` keeps the last known configuration for compatibility
with older firmware; sending an empty list removes all local I/O entities.

```json
{
  "local_io": [
    {"id": "relay_1", "type": "relay", "name": "Desk Lamp", "entity_id": "switch.waveshare_touch_lcd_8_desk_lamp", "legacy_entity_ids": ["switch.waveshare_touch_lcd_8_relay_1"]},
    {"id": "temperature_1", "type": "temperature", "name": "Case Temperature", "entity_id": "sensor.waveshare_touch_lcd_8_case_temperature", "legacy_entity_ids": ["sensor.waveshare_touch_lcd_8_temperature_1"], "unit": "°C", "precision": 1}
  ]
}
```

`entity_id` is optional for backwards compatibility. When present, it must use
the `switch` domain for relays or the `sensor` domain for temperature inputs.
The internal `id` stays stable for MQTT topics and Home Assistant's `unique_id`,
while the visible `entity_id` may follow the channel name. The Bridge migrates
known automatically generated IDs and leaves user-renamed registry IDs intact.
Firmware may provide `legacy_entity_ids` for explicit old IDs. The Bridge
validates and deduplicates these IDs and requires the same entity domain. When
multiple identical panels use the same suggested ID, Home Assistant adds a
numeric suffix such as `_2`; migrations preserve that suffix deterministically.

## Requirements

- Home Assistant 2025.11 or newer
- MQTT broker configured in Home Assistant
- [HomeTiles](https://github.com/GalusPeres/HomeTiles) firmware

Binary sensors require a HomeTiles firmware build with the dedicated Binary
Sensor tile (type 20). Flash that compatible firmware before upgrading the bridge
to v0.6.39 or newer because older firmware does not understand the structured
binary-state payload.

Camera popups require HomeTiles firmware v0.6.3 or newer. Camera support is
experimental: the bridge transcodes the selected Home Assistant camera into
display-sized JPEG frames, so CPU usage depends on the source stream, resolution,
frame rate and number of simultaneously open panels.

## Release Process

- Run `python -m unittest discover -s tests -v`
- Run `python -m compileall -q custom_components/tab5_lvgl tests`
- Bump `custom_components/tab5_lvgl/manifest.json` version
- Commit and push to `main`
- Push the matching `v*` tag (for example `v0.6.32`); GitHub Actions creates
  the release
- Never create the GitHub release manually; wait for the tag workflow to finish
- Replace the generated release text with the matching checked-in release notes,
  for example `gh release edit v0.6.34 --notes-file RELEASE_NOTES_v0.6.34.md`

## License

MIT License
