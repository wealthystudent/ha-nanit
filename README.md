# Nanit — Home Assistant Integration

<p align="center">
  <img src="custom_components/nanit/brand/icon@2x.png" alt="Nanit" width="128" />
</p>

<p align="center">
  <em>Keep an eye on your little one — right from your Home Assistant dashboard.</em>
</p>

> [!IMPORTANT]
> **v1.x is here.** The integration is now fully standalone — no add-on or Go daemon required. Just install, log in, and you're set. Previous 0.x.x releases (which required the `nanitd` add-on) are still available. See [LEGACY_INSTALL.md](LEGACY_INSTALL.md) for the old setup guide.

---

## Entities

| Type | Entities | Enabled by default |
|------|----------|--------------------|
| Camera | RTMPS live stream with on/off control | Yes |
| Sensor | Temperature, Humidity, Light level | Yes |
| Binary Sensor | Motion, Sound (cloud-polled) | Yes |
| Binary Sensor | Motion, Sound, Night mode, Connectivity (local push) | No |
| Switch | Night light, Camera power, Status LED, Mic mute | Night light, Camera power |
| Number | Volume (0–100 %) | No |

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
4. *(Optional)* Enter your camera's local IP for faster, LAN-first connectivity.

| Field | Required | Description |
|-------|----------|-------------|
| Email | Yes | Nanit account email |
| Password | Yes | Nanit account password |
| Store credentials | No | Saves credentials for easier re-auth |
| Camera IP | No | LAN IP of the camera (port 442) |

## How it works

The integration communicates directly with the Nanit cloud and (optionally) your camera over the local network. No intermediary services.

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

## Migrating from v0.x

1. Update the integration via HACS (or replace the files manually).
2. Delete the existing Nanit config entry in **Settings → Devices & Services**.
3. Re-add the integration. Entity unique IDs are preserved.
4. Uninstall the `nanitd` add-on — it's no longer needed.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Integration won't load | Check **Settings → System → Logs** and filter for `nanit`. |
| MFA code rejected | Codes expire quickly — use the latest one and finish setup promptly. |
| Stream not playing | Streams start on demand. Verify HA can reach `rtmps://media-secured.nanit.com`. |
| Sensors unavailable | The WebSocket may have dropped. It reconnects automatically — check logs. |
| Local connection failing | Confirm the camera IP is correct and port 442 is reachable from HA. |

## Requirements

- Home Assistant **2025.12** or newer
- A Nanit account with email/password
- HACS (recommended) or manual file access

## License

MIT
