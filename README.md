# Nanit — Home Assistant Integration

<p align="center">
  <img src="custom_components/nanit/brand/icon@2x.png" alt="Nanit" width="128" />
</p>

<p align="center">
  <em>Keep an eye on your little one — right from your Home Assistant dashboard.</em>
</p>

<a href="https://github.com/wealthystudent/ha-nanit">
  <img src="docs/star-banner.svg" alt="Star this repo to help us get an official Nanit API" width="100%" />
</a>

---

A custom [Home Assistant](https://www.home-assistant.io/) integration for [Nanit](https://www.nanit.com/) baby cameras. View live streams, monitor nursery conditions, control the night light, and automate your smart home — all without leaving Home Assistant.

## Supported devices

| Device | Status |
|--------|--------|
| Nanit Pro | Fully supported |
| Nanit Plus | Fully supported |
| Nanit Pro Camera (standalone) | Supported (no sound machine features) |
| Nanit Sound & Light Machine | Supported (local WebSocket or cloud relay) |

> [!NOTE]
> All Nanit cameras that work with the official Nanit app should work with this integration. If you have a model not listed above, please [open an issue](https://github.com/wealthystudent/ha-nanit/issues/new?template=bug_report.yml) to help us update this list.

## Entities

| Platform | Entity | Description | Enabled |
|----------|--------|-------------|---------|
| Camera | Camera | RTMPS live stream with on/off control. Supports HA Stream integration. | Yes |
| Sensor | Temperature | Nursery temperature in °C. | Yes |
| Sensor | Humidity | Relative humidity (%). | Yes |
| Sensor | Light Level | Ambient light in lux. | No |
| Binary Sensor | Motion | Motion detected (cloud-polled, 5-min window). | Yes |
| Binary Sensor | Sound | Sound detected (cloud-polled, 5-min window). | Yes |
| Binary Sensor | Connectivity | Camera-to-cloud connection status. | No |
| Switch | Night Light | Toggle the camera's built-in night light. | Yes |
| Switch | Camera Power | Toggle camera on/off (sleep mode). | Yes |
| Number | Volume | Camera speaker volume (0–100%). | No |

### Sound & Light Machine entities

If a Nanit Sound & Light Machine is linked to a camera, these additional entities appear on a separate device:

| Platform | Entity | Description | Enabled |
|----------|--------|-------------|---------|
| Switch | Power | Device power on/off. | Yes |
| Switch | Sound | Toggle sound playback. | Yes |
| Switch | Light | Toggle night light. | Yes |
| Select | Sound Track | Choose from available sound tracks. | Yes |
| Number | Volume | Sound volume (0–100%). | Yes |
| Number | Brightness | Light brightness (0–100%). | Yes |
| Sensor | Temperature | Device temperature in °C. | Yes |
| Sensor | Humidity | Relative humidity (%). | Yes |
| Sensor | Connection Mode | Diagnostic — shows Local, Cloud, or Unavailable. | No |
| Binary Sensor | S&L Connectivity | WebSocket connection status. | No |

S&L entities retain their last-known values when the WebSocket disconnects, rather than going "unavailable." This is an intentional design choice — it prevents entities from flashing "unavailable" during brief reconnection windows (e.g., the ~3-second hourly token refresh). The dedicated S&L Connectivity binary sensor and Connection Mode sensor report the actual connection state separately, so you can monitor connectivity without losing entity values.

Entities marked "No" under Enabled are created but disabled by default. Enable them in **Settings → Devices & Services → Nanit → Entities**.

> [!NOTE]
> Not all Nanit features are supported yet. Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Use cases

- **Nursery dashboard** — Camera stream, temperature, humidity, and motion on a single HA panel.
- **Temperature alerts** — Get notified if the nursery gets too hot or cold.
- **Motion-triggered automations** — Turn on lights, send a notification, or trigger a routine when motion is detected.
- **Bedtime routine** — Automate night light, volume, and camera power on a schedule.
- **Multi-camera overview** — Monitor multiple rooms at once if you have more than one Nanit camera.

## Installation

### HACS (recommended)

1. Open **HACS → Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/wealthystudent/ha-nanit` as **Integration**.
3. Install **Nanit**, then restart Home Assistant.

### Manual

Copy `custom_components/nanit/` into your HA `config/custom_components/` directory and restart.

## Setup

1. **Settings → Devices & Services → Add Integration → Nanit**
2. Sign in with your Nanit email and password.
3. Enter the MFA code sent to your device.
4. All cameras on your account are discovered automatically.

### Setup parameters

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| Email | Yes | — | Your Nanit account email. Used as the unique identifier for this config entry. |
| Password | Yes | — | Your Nanit account password. |
| Store credentials | No | Off | Saves your email and password so re-authentication can be completed without re-entering them. |
| MFA Code | Yes | — | One-time code sent to your device. Codes expire quickly — use the latest one. |

### Camera IP configuration (optional)

For faster LAN-first connectivity, configure a local IP per camera:

**Settings → Devices & Services → Nanit → Configure** → select camera → enter IP.

| Field | Required | Description |
|-------|----------|-------------|
| Camera | Yes (multi-camera only) | Which camera to configure. Skipped if you only have one. |
| Camera IP Address | No | Local IP (port 442). Leave blank for cloud-only mode. |
| Speaker IP Address | No | Local IP of a linked Sound & Light Machine (port 442). Leave blank to use cloud relay. |

When a camera IP is set, the integration connects directly over your LAN for sensor data and controls, using the cloud only for authentication and event detection. Clear the IP to return to cloud-only mode.

## How data is updated

The integration uses two update mechanisms — no unnecessary polling for real-time data:

| Data | Method | Interval | Source |
|------|--------|----------|--------|
| Temperature, humidity, light | **WebSocket push** | Instant (on change) | Camera (local or cloud) |
| Night light, volume, sleep mode | **WebSocket push** | Instant (on change) | Camera (local or cloud) |
| Camera connection status | **WebSocket push** | Instant (on change) | Camera (local or cloud) |
| Motion and sound events | **Cloud polling** | Every 30 seconds | Nanit cloud API |
| Live video stream | **On demand** | When viewed | RTMPS via Nanit media server |
| S&L state (light, sound, power) | **WebSocket push** | Instant (on change) | S&L device (local or cloud relay) |
| S&L temperature, humidity | **WebSocket push** | Instant (on change) | S&L device (local or cloud relay) |

Push data arrives via a persistent WebSocket connection to the camera. If the connection drops, it reconnects automatically and logs the event.

```
Home Assistant              Nanit Camera (LAN)        Nanit Cloud
┌────────────┐  WebSocket   ┌──────────┐
│ nanit      │◄────────────►│ :442     │
│ integration│              └──────────┘
│            │  WebSocket + REST                      ┌──────────────┐
│ aionanit   │◄──────────────────────────────────────►│ api.nanit.com│
└────────────┘                                        └──────────────┘
```

| Mode | Description |
|------|-------------|
| **Cloud only** | Default. All communication via Nanit cloud. |
| **Cloud + Local** | Cloud for auth and events, local WebSocket for sensors and controls. |

## Example automations

### Temperature alert

```yaml
automation:
  - alias: "Nursery too warm"
    trigger:
      - platform: numeric_state
        entity_id: sensor.nursery_temperature
        above: 24
        for: "00:05:00"
    action:
      - service: notify.mobile_app
        data:
          title: "Nursery Alert"
          message: "Temperature is {{ states('sensor.nursery_temperature') }}°C"
```

### Motion notification

```yaml
automation:
  - alias: "Nursery motion detected"
    trigger:
      - platform: state
        entity_id: binary_sensor.nursery_motion
        to: "on"
    action:
      - service: notify.mobile_app
        data:
          title: "Nursery"
          message: "Motion detected in the nursery"
```

### Bedtime routine

```yaml
automation:
  - alias: "Bedtime"
    trigger:
      - platform: time
        at: "19:30:00"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.nursery_night_light
      - service: number.set_value
        target:
          entity_id: number.nursery_volume
        data:
          value: 30
```

## Known limitations

| Limitation | Detail |
|------------|--------|
| **One account per integration** | Each config entry maps to a single Nanit account. Multiple accounts require multiple integration entries. |
| **Cloud dependency** | Authentication, motion/sound events, and live streaming always require the Nanit cloud — there is no fully offline mode. |
| **Self-signed TLS (local)** | Local camera connections use self-signed certificates. The integration accepts these automatically. |
| **RTMPS streaming** | Live video uses `rtmps://media-secured.nanit.com`. Your HA instance must be able to reach this address. |
| **Motion/sound is cloud-polled** | Motion and sound detection comes from the Nanit cloud API (polled every 30s), not from the camera directly. There may be up to ~30s delay. |
| **S&L stale values when offline** | Sound & Light entities show their last-known values when the device is disconnected, rather than going "unavailable." Check the S&L Connectivity binary sensor to confirm the device is actually reachable. This is intentional — it avoids disruptive flickers during brief reconnection windows. |
| **Cloud relay cannot detect S&L power-off** | When the Sound & Light Machine is connected via cloud relay (no local IP), powering off the device does not drop the WebSocket. The connectivity sensor continues showing "connected / cloud" until the connection times out. Local connections detect power-off immediately. |
| **Session expiry** | Nanit access tokens expire. The integration refreshes them automatically, but if refresh fails, a re-authentication notification will appear. |

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Integration won't load | Check **Settings → System → Logs** and filter for `nanit`. |
| MFA code rejected | Codes expire quickly — use the latest one and finish setup promptly. |
| Stream not playing | Streams start on demand. Verify HA can reach `rtmps://media-secured.nanit.com`. Check that the HA Stream integration is enabled. |
| Sensors unavailable | The WebSocket may have dropped. It reconnects automatically — check logs. If persistent, try reloading the integration. |
| Local connection failing | Confirm the camera IP is correct and port 442 is reachable from HA. Try pinging the IP. |
| Motion/sound always off | Verify your Nanit app shows events. Cloud events are polled every 30s with a 5-minute detection window. |
| Re-authentication required | Your session expired and auto-refresh failed. Click the notification to re-enter credentials. |
| Camera shows as unavailable | The camera may be offline or disconnected from Wi-Fi. Check the Connectivity binary sensor for status. |
| Diagnostics | **Settings → Devices & Services → Nanit → ⋮ → Download diagnostics** for a redacted debug report. |

## Removing the integration

1. Go to **Settings → Devices & Services → Nanit**.
2. Click the three-dot menu (⋮) → **Delete**.
3. Confirm deletion. All Nanit devices and entities are removed.

No leftover files or services remain after removal.

## Requirements

- Home Assistant **2025.12** or newer
- A Nanit account with email/password
- HACS (recommended) or manual file access

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions, code style, and the PR workflow. In short: branch from `main`, run `just check`, open a PR, and add a `release:patch`/`minor`/`major` label if it should trigger a beta release.

## License

[MIT](LICENSE)
