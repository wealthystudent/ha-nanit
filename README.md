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

1. **Go backend (`nanitd`)** must be running and accessible from your HA instance
2. **Nanit account** with email and password
3. **Home Assistant 2025.12+**
4. **HACS** (for easy installation) or manual copy

## Installation

### Option A: HACS (Recommended)

1. Open HACS in your HA instance
2. Click **⋮** (three dots top-right) → **Custom repositories**
3. Add `https://github.com/wealthystudent/ha-nanit` — Category: **Integration**
4. Search for "Nanit" in HACS and install
5. Restart Home Assistant

### Option B: Manual

1. Download this repository
2. Copy `custom_components/nanit/` into your HA `config/custom_components/` directory
3. Restart Home Assistant

## Setup

### 1. Start the Go Backend

The Go backend (`nanitd`) must be running before configuring the integration.

```bash
# Minimal (defaults: localhost:8080, auto-detect camera)
nanitd

# With config file
nanitd -config /path/to/config.yaml

# With environment variables
NANIT_HTTP_ADDR=":8080" \
NANIT_HLS_ENABLED=true \
NANIT_SESSION_PATH="/data/session.json" \
nanitd
```

Example `config.yaml`:

```yaml
nanit:
  api_base: "https://api.nanit.com"
  # camera_uid: ""   # auto-detected from account
  # baby_uid: ""     # auto-detected from account
  # camera_ip: ""    # set for local transport

http:
  addr: ":8080"

hls:
  enabled: true
  output_dir: "/tmp/nanit-hls"
  segment_time: 2
  playlist_size: 5

session:
  path: "/data/session.json"

log:
  level: info
  format: text
```

### 2. Add the Integration in HA

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **"Nanit"**
3. Enter your Nanit email and password
4. Enter the MFA code sent to your email/phone
5. Choose transport mode:
   - **Local only** — data from Go backend only
   - **Local + Cloud** — backend data + cloud events (motion/sound events)
6. Optionally set the Go backend URL (default: `http://localhost:8080`)

### 3. Verify

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

### Integration fails to load
- Ensure `nanitd` is running and accessible at the configured URL
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
