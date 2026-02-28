# Nanit Baby Camera — Home Assistant Integration

<p align="center">
  <img src="custom_components/nanit/brand/icon@2x.png" alt="Nanit" width="128" />
</p>

A Home Assistant custom integration for Nanit baby cameras. Pure Python — no add-on required.

Keep an eye on your little one with this custom [Home Assistant](https://www.home-assistant.io/) integration for [Nanit](https://www.nanit.com/) baby cameras. It connects directly to the Nanit cloud API and optionally to your camera's local network, with no external daemon or add-on needed.

## What's in the Crib?

| Entity Type | Entities | Default Enabled |
|-------------|----------|-----------------|
| Sensor | Temperature, Humidity, Light (lux) | Temp, Humidity |
| Binary Sensor | Cloud Motion, Cloud Sound (cloud polling, 5-min window) | Yes |
| Binary Sensor | Motion, Sound, Night mode, Connectivity (local push) | No |
| Switch | Night light, Status LED, Mic mute | Night light |
| Number | Volume (0-100%) | No |
| Camera | RTMPS live stream with on/off control | Yes |

## Quick Start

1. **Install the Integration**: Find **Nanit** in HACS (add the repo URL as a custom integration) or copy the files manually.
2. **Configure**: Add the **Nanit** integration in Home Assistant, sign in, and enter your MFA code.
3. **Optional**: Enter your camera's local IP address for faster, local-first connectivity.

That's it — no add-on, no Docker container, no Go daemon.

## Features

### Cloud + Local Connectivity

The integration connects to the Nanit cloud API for authentication, baby metadata, and cloud events. If you provide your camera's local IP address, it also establishes a direct WebSocket connection for faster sensor updates and lower latency.

| Mode | How It Works | When to Use |
|------|-------------|-------------|
| **Cloud only** | All communication via `api.nanit.com` | Default, works everywhere |
| **Cloud + Local** | Cloud for auth/events, local WebSocket for sensors/controls | Faster updates, lower latency |

Local connections use the camera's self-signed TLS certificate (port 442). The integration handles this automatically.

### Camera & Streaming

- **RTMPS live stream**: View your camera feed directly in Home Assistant.
- **Snapshot**: Capture still images from the camera.
- **Camera power**: Toggle standby mode via the camera entity's on/off control.

### Sensors & Controls

- **Environment sensors**: Temperature, humidity, and light level — pushed from the camera in real time.
- **Motion & Sound detection**: Local (from camera push) and cloud (from Nanit event polling).
- **Night light**: Toggle the camera's built-in night light.
- **Volume**: Adjust the camera speaker volume (0-100%).
- **Status LED & Mic mute**: Configure camera indicators and microphone.

## Installation

### Prerequisites

- A Nanit account with email and password.
- Home Assistant 2025.12 or newer.
- HACS installed (recommended) or the ability to copy files manually.

### Option A: HACS (Recommended)

1. Open HACS and click the three dots in the top-right corner.
2. Select **Custom repositories**.
3. Add `https://github.com/wealthystudent/ha-nanit` with the category **Integration**.
4. Search for **Nanit** in HACS and install it.
5. Restart Home Assistant.

### Option B: Manual Copy

1. Download this repository.
2. Copy the `custom_components/nanit/` folder into your `config/custom_components/` directory.
3. Restart Home Assistant.

## Configuration

### Initial Setup

1. Go to **Settings > Devices & Services > Add Integration**.
2. Search for **Nanit**.
3. Enter your Nanit email and password.
4. Provide the MFA code sent to your device.
5. Optionally enter your camera's local IP address for direct LAN connectivity.

### Setup Fields

| Field | Required | Description |
|-------|----------|-------------|
| Email | Yes | Your Nanit account email |
| Password | Yes | Your Nanit account password |
| Store credentials | No | Saves credentials for easier re-authentication |
| Camera IP | No | Direct LAN IP of your camera for local-first connectivity |

### Adjusting Settings

Click **Configure** on the Nanit integration page to update the camera IP address. You can also change it in the integration's **Options** menu.

## Migrating from v0.x

Version 1.0 is a complete rewrite. The Go add-on (`nanitd`) is no longer needed.

### Migration Steps

1. **Update the integration** via HACS or manual copy.
2. **Remove the old config entry**: Go to **Settings > Devices & Services**, find Nanit, and delete it.
3. **Re-add the integration**: Follow the setup steps above. Your entities will be recreated with the same unique IDs.
4. **Stop the add-on** (optional): If you were running the Nanit Daemon add-on, you can uninstall it.

### What Changed

- No more Go daemon or Docker container — everything runs natively in Python.
- Authentication and camera communication happen directly from the integration.
- Camera streaming uses RTMPS instead of HLS proxy.
- Sensor data is pushed in real time via WebSocket instead of polled from an HTTP API.
- The `transport` configuration option is gone — local connectivity is controlled by the camera IP field.

## Architecture

```
┌──────────────────────┐                    ┌──────────────┐
│  Home Assistant       │  WebSocket (wss)   │ Nanit Camera │
│  custom_components/   │ ◄────────────────► │ (local:442)  │
│  nanit/               │                    └──────────────┘
│                       │
│  aionanit (PyPI)      │  WebSocket (wss)   ┌──────────────┐
│  - Auth & tokens      │ ◄────────────────► │ Nanit Cloud  │
│  - Camera control     │  HTTPS (REST)      │ api.nanit.com│
│  - Protobuf codec     │ ◄────────────────► │              │
└──────────────────────┘                    └──────────────┘
```

The integration uses [`aionanit`](packages/aionanit/), a pure-Python async client library that handles:

- **Authentication**: Email/password login, MFA, automatic token refresh.
- **WebSocket**: Protobuf-over-WebSocket communication with the camera (cloud and local).
- **REST API**: Baby metadata, cloud events, snapshots.
- **Streaming**: RTMPS URL construction for live video.

## Troubleshooting

**Integration not loading**
Check Home Assistant logs under **Settings > System > Logs** (filter for `nanit` or `aionanit`).

**MFA difficulties**
MFA codes expire quickly. Use the most recent code and complete setup promptly.

**Stream not playing**
The stream starts on demand — give it a few seconds. If it fails, check that your HA instance can reach `rtmps://media-secured.nanit.com`.

**Sensors unavailable**
If sensors show as unavailable, the WebSocket connection to the camera may have dropped. Check logs for reconnection messages. The integration reconnects automatically with exponential backoff.

**Local connection issues**
If you entered a camera IP but sensors aren't updating locally, verify the IP is correct and that port 442 is reachable from your HA instance. The integration falls back to cloud automatically.

## Development

### Running Tests (aionanit)

```bash
cd packages/aionanit
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

### Project Structure

```
ha-nanit/
├── custom_components/nanit/    # HA integration
│   ├── __init__.py             # Entry setup/teardown
│   ├── config_flow.py          # UI config flow
│   ├── coordinator.py          # Push + polling coordinators
│   ├── hub.py                  # Camera lifecycle management
│   ├── camera.py               # Camera entity (RTMPS stream)
│   ├── sensor.py               # Environment sensors
│   ├── binary_sensor.py        # Motion/sound/connectivity
│   ├── switch.py               # Night light, power, LED, mic
│   ├── number.py               # Volume control
│   └── diagnostics.py          # Redacted debug output
├── packages/aionanit/          # Async Nanit client library
│   ├── aionanit/
│   │   ├── auth.py             # Token management
│   │   ├── rest.py             # REST API client
│   │   ├── camera.py           # Camera state + commands
│   │   ├── client.py           # Top-level client
│   │   ├── models.py           # Data models
│   │   ├── proto/              # Protobuf types (betterproto)
│   │   └── ws/                 # WebSocket transport
│   └── tests/                  # 135 unit tests
└── nanitd/                     # Legacy Go daemon (deprecated)
```

## License

MIT
