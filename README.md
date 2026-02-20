# Nanit Baby Camera — Home Assistant Integration

<p align="center">
  <img src="custom_components/nanit/icon@2x.png" alt="Nanit" width="128" />
</p>

A Home Assistant integration for Nanit baby cameras. Local-first, cloud-optional.

Keep an eye on your little one with this custom [Home Assistant](https://www.home-assistant.io/) integration for [Nanit](https://www.nanit.com/) baby cameras. It's powered by a Go backend daemon (`nanitd`) to give you fast, reliable access to your nursery.

## What's in the Crib?

| Entity Type | Entities | Default Enabled |
|-------------|----------|-----------------|
| Sensor | Temperature, Humidity, Light (lux) | Temp, Humidity, Light |
| Binary Sensor | Motion, Sound, Night mode, Connectivity | Connectivity |
| Switch | Night light, Sleep mode, Status LED, Mic mute | Night light |
| Number | Volume (0-100%) | No |
| Camera | HLS live stream with on/off control | Yes |
| Event | Activity (motion + sound with timestamps, cloud only) | Yes |

## Quick Start

Setting up your nursery is easy:

1. **Install the Add-on**: Add `https://github.com/wealthystudent/ha-nanit` to your Add-on Store and install the **Nanit Daemon**.
2. **Install the Integration**: Find **Nanit** in HACS (add the same repo URL as a custom integration) or copy the files manually.
3. **Configure**: Add the **Nanit** integration in Home Assistant, sign in, and enter your MFA code.

## Features in Detail

### Transport Modes

Choose how your data travels from the nursery to Home Assistant. You can change this anytime in the integration configuration.

| Mode | Description | Entities |
|------|-------------|----------|
| **Local** | All data comes directly from the Go backend. No cloud polling involved. | All except Event |
| **Local + Cloud** | Combines backend data with Nanit cloud events for motion and sound. | All entities |

### Naptime and More
- **Sleep mode**: Puts the camera to rest when it's not needed.
- **Night light**: Soft illumination for those midnight check-ins.
- **Local-first**: Direct LAN connection support via Camera IP to bypass cloud relays.

## Setting Up the Nursery

### Prerequisites
- A Nanit account with an email and password.
- Home Assistant 2025.12 or newer.
- HACS installed (recommended) or the ability to copy files manually.

### Detailed Installation

#### 1. Install the Nanit Daemon Add-on
The Go backend runs as a Home Assistant add-on, so there's no need for a separate machine.

1. Go to **Settings > Add-ons > Add-on Store**.
2. Click the three dots in the top-right corner and select **Repositories**.
3. Add `https://github.com/wealthystudent/ha-nanit`.
4. Find **Nanit Daemon** in the store and click **Install**.
5. Start the add-on. It will wait for your credentials once you set up the integration.

*Note: If you want to run `nanitd` on a different machine, skip this and provide the host URL during setup.*

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
3. If the add-on is running, it should be detected automatically.
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
| `/api/settings` | GET | Night light, sleep mode, volume, and other settings |
| `/api/events` | GET | Recent motion and sound events |
| `/api/snapshot` | GET | JPEG still image from camera |
| `/api/hls/status` | GET | HLS proxy status |
| `/api/hls/start` | POST | Start the HLS stream proxy |
| `/api/hls/stop` | POST | Stop the HLS stream proxy |
| `/api/control/nightlight` | POST | Toggle the night light |
| `/api/control/sleep` | POST | Toggle sleep mode |
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

## License

MIT

