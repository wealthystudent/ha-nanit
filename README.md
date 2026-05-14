# Nanit — Home Assistant Integration

<p align="center">
  <img src="custom_components/nanit/brand/icon@2x.png" alt="Nanit" width="128" />
</p>

<a href="https://github.com/wealthystudent/ha-nanit">
  <img src="docs/star-banner.svg" alt="Star this repo to help us get an official Nanit API" width="100%" />
</a>

---

> **Monitor your baby — right from Home Assistant.**
>
> Live streams, nursery sensors, night light control, and automations — all from your HA dashboard. Works with all Nanit cameras and the Sound & Light Machine.

<p align="center">
  <img src="docs/images/nanit-card.png" alt="Nanit dashboard card" width="420" />
</p>

## Requirements

- Home Assistant **2025.12** or newer
- A Nanit account with email/password
- [HACS](https://hacs.xyz/) (recommended)

## Installation

### HACS (recommended)

1. Open **HACS → Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/wealthystudent/ha-nanit` as **Integration**.
3. Install **Nanit**, then restart Home Assistant.

### Manual

Copy `custom_components/nanit/` into your HA `config/custom_components/` directory and restart.

## Setup

1. Go to **Settings → Devices & Services → Add Integration → Nanit**.
2. Enter your Nanit email and password.
3. Enter the MFA code sent to your device (use the latest code — they expire quickly).
4. Done — all cameras on your account are discovered automatically.

> [!TIP]
> Enable **Store credentials** during setup so re-authentication can happen without re-entering your password.

## What you get

**Per camera:**
- 📷 Live camera stream (RTMPS)
- 🌡️ Temperature & humidity sensors
- 👁️ Motion & sound detection
- 💡 Night light switch
- 🔌 Camera power switch

**Sound & Light Machine** (if linked):
- Power, sound, and light switches
- Sound track selector, volume & brightness controls
- Temperature & humidity sensors

Some entities are disabled by default. Enable them in **Settings → Devices & Services → Nanit → Entities**.

## Dashboard Card

A companion Lovelace card is **bundled with the integration** — no HACS frontend dependencies or manual JS installation required. After setup, the card appears in your card picker automatically.

**To add it:** Open any dashboard → **Add Card** → search for **Nanit** → select your camera.

The card provides:
- Live camera stream with loading indicator
- Temperature & humidity overlays
- Motion & sound activity indicators
- Night light slider (drag to adjust brightness, 0% = off)
- Sound machine controls with icon-based track selection
- Volume slider
- Network info popup (WiFi name, frequency, signal strength)

> [!NOTE]
> If your Lovelace is in **YAML mode**, add the resource manually:
> ```yaml
> resources:
>   - url: /nanit-card/nanit-card.js
>     type: module
> ```

## Local connection (optional)

For faster response times, you can connect directly to your camera over LAN:

**Settings → Devices & Services → Nanit → Configure** → select camera → enter its local IP address.

The integration will use your local network for sensors and controls, falling back to cloud for auth and events.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| MFA code rejected | Codes expire fast — use the latest one. |
| Stream not playing | Verify HA can reach `rtmps://media-secured.nanit.com` and the Stream integration is enabled. |
| Sensors unavailable | WebSocket reconnects automatically. Try reloading the integration if it persists. |
| Local connection failing | Confirm the camera IP is correct and port 442 is reachable from HA. |
| Re-authentication required | Session expired — click the notification to re-enter credentials. |
| Other issues | Check **Settings → System → Logs** (filter for `nanit`) or download diagnostics from the integration page. |

## Known limitations

- Authentication, motion/sound events, and streaming always require the Nanit cloud — no fully offline mode.
- Motion and sound detection is polled every 30 seconds (up to ~30s delay).
- Live video requires your HA instance to reach `rtmps://media-secured.nanit.com`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and PR workflow.

## License

[MIT](LICENSE)
