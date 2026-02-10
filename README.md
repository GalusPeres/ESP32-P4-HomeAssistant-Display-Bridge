# Tab5 LVGL MQTT Bridge

Home Assistant custom integration for Tab5 LVGL displays via MQTT.

## Features

- MQTT-based communication with Tab5 LVGL displays
- Automatic entity synchronization
- Scene control
- Sensor state publishing
- Light and switch control
- Auto-discovery support

## Installation

### Via HACS (Recommended)

1. Add this repository as a custom repository in HACS:
   - HACS → Integrations → ⋮ (top right) → Custom repositories
   - Repository: `https://github.com/GalusPeres/ha-tab5-mqtt-bridge`
   - Category: Integration
   - Click "Add"

2. Install the integration:
   - HACS → Integrations → Search for "Tab5 LVGL"
   - Click "Download"

3. Restart Home Assistant

4. Add the integration:
   - Settings → Devices & Services → Add Integration
   - Search for "Tab5 LVGL"

### Manual Installation

1. Copy the `custom_components/tab5_lvgl` directory to your Home Assistant `custom_components` folder
2. Restart Home Assistant
3. Add the integration via Settings → Devices & Services

## Configuration

Configure via the Home Assistant UI:
- **Base Topic**: MQTT base topic (default: `tab5`)
- **HA Prefix**: Home Assistant state stream prefix (default: `ha/statestream`)
- **Sensors**: Entities to sync to the display
- **Lights**: Lights to control from the display
- **Switches**: Switches to control from the display
- **Scenes**: Scenes controllable from the display

## Light & Switch Commands

Tab5 can control entities by publishing to the command topics:

- `base_topic/cmnd/light`
- `base_topic/cmnd/switch`

Payload formats:

- JSON: `{"entity_id":"light.kitchen","state":"on","brightness":128}`
- Simple: `light.kitchen on` or `on` (if only one entity is configured)

## Requirements

- Home Assistant 2025.11 or newer
- MQTT broker configured in Home Assistant
- Tab5 LVGL device with MQTT support

## Changelog

### 0.2.37 (2026-02-10)
- Version bump with `v` tag release for HACS compatibility refresh

### 0.2.36 (2026-02-10)
- Version bump for HACS update detection

### 0.2.35 (2026-02-10)
- Add native battery sensor entity (`sensor.tab5_batterie`) from `base_topic/sensor/soc_pct`
- Publish live Tab5 battery SoC snapshot at MQTT connect and every telemetry cycle
- Extend manifest metadata for HACS (`issue_tracker`, `integration_type`)

### 0.2.11 (2025-12-22)
- Use state history API for full 5-minute buckets

### 0.2.10 (2025-12-22)
- Use recorder state history for 24h/5min charts (288 points)

### 0.2.9 (2025-12-22)
- Fallback to state history for finer sensor history charts

### 0.2.8 (2025-12-22)
- Fix history stats query for newer Home Assistant recorder API

### 0.2.7 (2025-12-22)
- Added history request/response support for sensor popups (24h/5min aggregation)

### 0.2.6 (2025-12-21)
- Publish light brightness and supported modes in state updates

### 0.2.5 (2025-12-19)
- Publish light color in state updates for display/web UI

### 0.2.4 (2025-12-19)
- Added light and switch control support
- Added light/switch entity metadata in MQTT config

### 0.2.3 (2025-12-04)
- Added debug logging for MQTT config payload
- Investigating scene transmission issue

### 0.2.2 (2025-12-04)
- Second test release for HACS updates
- Documentation improvements

### 0.2.1 (2025-12-04)
- Test release for HACS update mechanism
- Minor improvements

### 0.2.0 (2025-12-04)
- Fixed compatibility with Home Assistant 2025.11 and 2025.12
- Updated selector API to new format
- Removed deprecated config_entry handling
- Added MQTT as explicit dependency

### 0.1.0
- Initial release

## Support

For issues and feature requests, please use the [GitHub issue tracker](https://github.com/GalusPeres/ha-tab5-mqtt-bridge/issues).

## License

MIT License
