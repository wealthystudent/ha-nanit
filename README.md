# Nanit Baby Camera — Home Assistant Integration

<p align="center">
  <img src="custom_components/nanit/icon@2x.png" alt="Nanit" width="128" />
</p>

A Home Assistant integration for Nanit baby cameras. Local-first, cloud-optional.

Keep an eye on your little one with this custom [Home Assistant](https://www.home-assistant.io/) integration for [Nanit](https://www.nanit.com/) baby cameras. It's powered by a Go backend daemon (`nanitd`) to give you fast, reliable access to your nursery.

## What's in the Crib?

| Entity Type | Entities | Default Enabled |
|-------------|----------|-----------------|
| Sensor | Temperature, Humidity, Light (lux) | Temp, Humidity |
| Binary Sensor | Cloud Motion, Cloud Sound (cloud polling, 5-min window) | Yes |
| Binary Sensor | Motion, Sound, Night mode, Connectivity (local) | No |
| Switch | Night light, Status LED, Mic mute | Night light |
| Number | Volume (0-100%) | No |
| Camera | HLS live stream with on/off control | Yes |

## Quick Start

Setting up your nursery is easy:

1. **Install the Daemon**: Run the **Nanit Daemon** as a Home Assistant add-on (HA OS / Supervised) or as a standalone Docker container (HA Container / Core).
2. **Install the Integration**: Find **Nanit** in HACS (add the same repo URL as a custom integration) or copy the files manually.
3. **Configure**: Add the **Nanit** integration in Home Assistant, sign in, and enter your MFA code.

## Features in Detail

### Transport Modes

Choose how your data travels from the nursery to Home Assistant. You can change this anytime in the integration configuration.

| Mode | Description | Entities |
|------|-------------|----------|
| **Local** | All data comes directly from the Go backend. No cloud polling involved. | All except Cloud Motion/Sound |
| **Local + Cloud** | Combines backend data with Nanit cloud polling for motion and sound detection. | All entities |

### Naptime and More
- **Camera power**: Use the camera entity's on/off control to enter or exit standby.
- **Night light**: Soft illumination for those midnight check-ins.
- **Local-first**: Direct LAN connection support via Camera IP to bypass cloud relays.

## Setting Up the Nursery

### Prerequisites
- A Nanit account with an email and password.
- Home Assistant 2025.12 or newer.
- HACS installed (recommended) or the ability to copy files manually.
- **For add-on installs**: Home Assistant OS or a Supervised installation.
- **For standalone installs**: Docker available on a machine reachable by Home Assistant.

### Detailed Installation

#### 1. Install the Nanit Daemon

The Go backend (`nanitd`) handles camera communication, sensor data, and HLS streaming. Choose the option that matches your Home Assistant installation type.

**Option A: Home Assistant Add-on (HA OS / Supervised)**

If you run Home Assistant OS or a Supervised install, install `nanitd` as an add-on:

1. Go to **Settings > Add-ons > Add-on Store**.
2. Click the three dots in the top-right corner and select **Repositories**.
3. Add `https://github.com/wealthystudent/ha-nanit`.
4. Find **Nanit Daemon** in the store and click **Install**.
5. Start the add-on. It will wait for your credentials once you set up the integration.

**Option B: Standalone Docker Container (HA Container / Core)**

If you run Home Assistant Container or Core (no Supervisor), run `nanitd` as a standalone Docker container:

```bash
docker run -d \
  --name nanitd \
  --restart unless-stopped \
  -p 8080:8080 \
  -v nanitd-data:/data \
  -e NANIT_HTTP_ADDR="0.0.0.0:8080" \
  -e NANIT_SESSION_PATH="/data/session.json" \
  -e NANIT_HLS_ENABLED="true" \
  -e NANIT_HLS_OUTPUT_DIR="/tmp/nanit-hls" \
  -e NANIT_LOG_LEVEL="info" \
  ghcr.io/wealthystudent/nanitd-amd64:0.5.1 \
  /usr/bin/nanitd
```

On a Raspberry Pi or other ARM64 device, use the `nanitd-aarch64` image instead:

```bash
docker run -d \
  --name nanitd \
  --restart unless-stopped \
  -p 8080:8080 \
  -v nanitd-data:/data \
  -e NANIT_HTTP_ADDR="0.0.0.0:8080" \
  -e NANIT_SESSION_PATH="/data/session.json" \
  -e NANIT_HLS_ENABLED="true" \
  -e NANIT_HLS_OUTPUT_DIR="/tmp/nanit-hls" \
  -e NANIT_LOG_LEVEL="info" \
  ghcr.io/wealthystudent/nanitd-aarch64:0.5.1 \
  /usr/bin/nanitd
```

Optional environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `NANIT_HTTP_ADDR` | `:8080` | Listen address for the HTTP API |
| `NANIT_SESSION_PATH` | `/data/session.json` | Path to persist auth tokens |
| `NANIT_LOG_LEVEL` | `info` | Log level (`debug`, `info`, `warning`, `error`) |
| `NANIT_HLS_ENABLED` | `false` | Enable HLS live stream proxy |
| `NANIT_HLS_OUTPUT_DIR` | (none) | Directory for HLS segments |
| `NANIT_CAMERA_IP` | (none) | Camera LAN IP for direct local connection |

After starting the container, provision auth tokens with the login helper:

```bash
just login --host http://<docker-host>:8080
```

Verify it's running: `curl http://<docker-host>:8080/api/status`

#### 2. Install the Integration

**Option A: HACS (Recommended)**
1. Open HACS and click the three dots in the top-right corner.
2. Select **Custom repositories**.
3. Add `https://github.com/wealthystudent/ha-nanit` with the category **Integration**.
4. Search for **Nanit** in HACS and install it.
5. Restart Home Assistant.

**Option B: Manual Copy**
1. Download this repository.
2. Copy the `custom_components/nanit/` folder into your `config/custom_components/` directory.
3. Restart Home Assistant.

#### 3. Configure the Integration
1. Go to **Settings > Devices & Services > Add Integration**.
2. Search for **Nanit**.
3. If the add-on is running, it should be detected automatically. If you're running `nanitd` standalone (Option B above), uncheck **Use detected add-on** or enter the backend URL (e.g., `http://192.168.1.50:8080`) when prompted.
4. Enter your Nanit email and password.
5. Provide the MFA code sent to your device.
6. Pick your transport mode (Local or Local + Cloud).
7. Optional: Enter your camera's local IP address for a direct LAN connection.

## Configuration Options

### Initial Setup Fields
| Field | Required | Description |
|-------|----------|-------------|
| Email | Yes | Your Nanit account email |
| Password | Yes | Your Nanit account password |
| Store credentials | No | Saves credentials for easier re-authentication |
| Transport | Yes | Local only or Local + Cloud |
| Camera IP | No | Direct LAN IP of your camera to bypass cloud relays |
| Go Backend URL | No | URL of the `nanitd` instance (e.g., `http://192.168.1.50:8080`). Only shown when not using the add-on. Defaults to `http://localhost:8080` |

### Adjusting Settings
Click **Configure** on the Nanit integration page to update:
- **Go Backend URL**: If you're running `nanitd` externally.
- **Transport mode**: Switch between local and cloud-enhanced modes.
- **Camera IP**: Set or change the camera's local IP address.

## Advanced

### Architecture
The integration uses a Python component for Home Assistant logic and a Go daemon for high-performance camera handling.

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

### Technical Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Connection status and baby information |
| `/api/sensors` | GET | Temperature, humidity, light, motion, and sound |
| `/api/settings` | GET | Night light, camera power (sleep), volume, and other settings |
| `/api/events` | GET | Recent motion and sound events |
| `/api/snapshot` | GET | JPEG still image from camera |
| `/api/hls/status` | GET | HLS proxy status |
| `/api/hls/start` | POST | Start the HLS stream proxy |
| `/api/hls/stop` | POST | Stop the HLS stream proxy |
| `/api/control/nightlight` | POST | Toggle the night light |
| `/api/control/sleep` | POST | Toggle camera power (sleep/standby) |
| `/api/control/volume` | POST | Set volume (0-100) |
| `/api/control/mic` | POST | Toggle microphone mute |
| `/api/control/statusled` | POST | Toggle the status LED |
| `/api/auth/status` | GET | Add-on authentication and ready status |
| `/api/auth/token` | POST | Provision authentication tokens to the add-on |

### Helpful Tips

**Add-on issues**
Check the logs at **Settings > Add-ons > Nanit Daemon > Log**. Ensure your system has enough memory for `nanitd` and `ffmpeg`.

**Integration loading**
Make sure the add-on is running and check the Home Assistant logs under **Settings > System > Logs** (filter for `nanit`).

**MFA difficulties**
MFA codes expire quickly. Ensure you use the most recent code and complete the setup as soon as you receive it.

**Stream performance**
The stream starts when you open it, so give it a few seconds to warm up. You can verify the HLS endpoint with `curl http://<addon-host>:8080/api/hls/status` and ensure `ffmpeg` is available.

**Unavailable sensors**
If sensors go dark, check that `nanitd` can reach the camera in the add-on logs. You can also check the status via `curl http://<addon-host>:8080/api/status` to see if `connected` is true.

## Manual API Testing

The cloud binary sensors read from the add-on endpoint `GET /api/events` (see `custom_components/nanit/api.py` and `custom_components/nanit/coordinator.py`).
To inspect the raw response structure, use the helper scripts in `tools/` via `just`:

```bash
# Install Python dependencies (first time only)
pip install requests

# Login to Nanit cloud (interactive: email + password + MFA)
just login

# Fetch recent events (default: 10)
just events

# Fetch a specific number of events
just events --limit 5
```

### Login + Token Provisioning (same flow as the integration)

If you want to run `nanitd` locally, use the login helper to authenticate with Nanit
and provision the tokens to your local daemon. This uses the same auth client as
the Home Assistant integration (`custom_components/nanit/api.py`).

```bash
# Start nanitd locally (port 8080)
cd nanitd/src
go build ./...
NANIT_HTTP_ADDR="0.0.0.0:8080" NANIT_SESSION_PATH="/tmp/nanit-session.json" ./nanitd

# In another terminal: login and provision tokens to nanitd
just login --host http://localhost:8080

# Now test events
just events
just events --limit 5
```

If you only want tokens without provisioning:

```bash
just login --no-provision --json
```

If you see a connection error, nanitd isn't running or the host is wrong.
Either start nanitd locally, use the add-on host, or pass `--no-provision`
to only fetch tokens.

## License

MIT
