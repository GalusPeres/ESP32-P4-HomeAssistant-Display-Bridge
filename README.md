# Tab5 LVGL MQTT Bridge

Home Assistant custom integration for Tab5 LVGL displays via MQTT.

## Features

- MQTT-based communication with Tab5 LVGL displays
- Automatic entity synchronization
- Scene control
- Sensor state publishing
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
- **Scenes**: Scenes controllable from the display

## Requirements

- Home Assistant 2025.11 or newer
- MQTT broker configured in Home Assistant
- Tab5 LVGL device with MQTT support

## Changelog

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
