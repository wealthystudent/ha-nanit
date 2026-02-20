# Nanit Baby Camera — Home Assistant Integration

Custom [Home Assistant](https://www.home-assistant.io/) integration for [Nanit](https://www.nanit.com/) baby cameras, powered by a Go backend daemon (`nanitd`).

## Architecture

```
┌──────────────────────┐     HTTP REST      ┌──────────────────┐     WebSocket     ┌──────────────┐
│  Home Assistant       │ ◄────────────────► │  nanitd (Go)     │ ◄───────────────► │ Nanit Camera │
│  custom_components/   │   localhost:8080   │  Sensors, HLS,   │                   │ (local/cloud)│
│  nanit/               │                    │  Camera control   │                   └──────────────┘
└──────────────────────┘                    └──────────────────┘
        │                                          │
        │  Direct HTTPS                            │ RTMPS → HLS
        ▼                                          ▼
┌──────────────────┐                    ┌──────────────────┐
│ api.nanit.com    │                    │ HLS Stream       │
│ Auth, MFA, Babies│                    │ /hls/stream.m3u8 │
└──────────────────┘                    └──────────────────┘
```

- **Python integration** handles HA config flow, entity management, and Nanit cloud authentication (email + password + MFA)
- **Go backend (`nanitd`)** handles camera WebSocket connections, sensor data, HLS streaming, and camera controls

## Features

| Entity Type | Entities | Default Enabled |
|-------------|----------|-----------------|
| Sensor | Temperature (°C), Humidity (%), Light (lux) | Temp ✅, Humidity ✅, Light ❌ |
| Binary Sensor | Motion, Sound, Night mode, Connectivity | Motion ✅, Sound ✅, others ❌ |
| Switch | Night light, Sleep mode, Status LED, Mic mute | Night light ✅, others ❌ |
| Number | Volume (0-100%) | ❌ |
| Camera | HLS live stream with on/off control | ✅ |
| Event | Motion events, Sound events (cloud only) | ✅ |

## Prerequisites

1. **Nanit account** with email and password
2. **Home Assistant 2025.12+** (HA OS recommended for add-on support)
3. **HACS** (for easy installation) or manual copy

## Installation

### Step 1: Install the Nanit Daemon Add-on

The Go backend runs as an HA add-on so you don't need a separate machine.

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click **⋮** (three dots top-right) → **Repositories**
3. Add: `https://github.com/wealthystudent/ha-nanit`
4. Find **"Nanit Daemon"** in the store and click **Install**
5. Start the add-on

> **Alternative:** If you prefer running `nanitd` on a separate machine, skip this step and provide the host URL during integration setup.

### Step 2: Install the Integration

#### Option A: HACS (Recommended)

1. Open HACS in your HA instance
2. Click **⋮** (three dots top-right) → **Custom repositories**
3. Add `https://github.com/wealthystudent/ha-nanit` — Category: **Integration**
4. Search for "Nanit" in HACS and install
5. Restart Home Assistant

#### Option B: Manual

1. Download this repository
2. Copy `custom_components/nanit/` into your HA `config/custom_components/` directory
3. Restart Home Assistant

### Step 3: Configure the Integration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **"Nanit"**
3. If the Nanit Daemon add-on is running, you'll be asked to use it (recommended — just click submit)
4. Enter your Nanit email and password
5. Enter the MFA code sent to your email/phone
6. Choose transport mode:
   - **Local only** — data from Go backend only
   - **Local + Cloud** — backend data + cloud events (motion/sound events)

> If the add-on is not detected (e.g., running `nanitd` externally), you'll also see a field for the Go backend URL.

### Verify

After setup, you should see a new "Nanit" device with sensors, switches, camera, etc. under **Settings → Devices & Services → Nanit**.

## Transport Modes

| Mode | Description | Entities |
|------|-------------|----------|
| **Local** | All data from Go backend. No cloud polling. | All except Event |
| **Local + Cloud** | Backend data + Nanit cloud events. | All entities |

You can change the transport mode anytime via **Options** on the integration page.

## Configuration Options

After initial setup, click **Configure** on the Nanit integration to change:

- **Transport mode** — Switch between local and local+cloud

## Troubleshooting

### Add-on won't start
- Check add-on logs: **Settings → Add-ons → Nanit Daemon → Log**
- Ensure your HA instance has enough memory (nanitd + ffmpeg)

### Integration fails to load
- Ensure the nanitd add-on (or external `nanitd`) is running
- Check HA logs: **Settings → System → Logs** and filter for `nanit`

### MFA code not accepted
- Make sure you're entering the most recent code
- MFA codes expire quickly — complete the setup promptly

### Camera stream not working
- Ensure HLS is enabled in `nanitd` config (`hls.enabled: true`)
- Verify the HLS endpoint: `curl http://localhost:8080/api/hls/status`
- Check that `ffmpeg` is available on the machine running `nanitd`

### Sensors showing "unavailable"
- Verify `nanitd` can reach the Nanit camera (check `nanitd` logs)
- Check `curl http://localhost:8080/api/status` — should show `"connected": true`

## Go Backend API Reference

The integration communicates with `nanitd` via these endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Connection status, baby info |
| `/api/sensors` | GET | Temperature, humidity, light, motion, sound |
| `/api/settings` | GET | Night light, sleep mode, volume, etc. |
| `/api/events` | GET | Recent events |
| `/api/hls/status` | GET | HLS proxy status |
| `/api/control/nightlight` | POST | Toggle night light |
| `/api/control/sleep` | POST | Toggle sleep mode |
| `/api/control/volume` | POST | Set volume (0-100) |
| `/api/control/mic` | POST | Toggle mic mute |
| `/api/control/statusled` | POST | Toggle status LED |
| `/api/hls/start` | POST | Start HLS stream proxy |
| `/api/hls/stop` | POST | Stop HLS stream proxy |

## License

MIT
