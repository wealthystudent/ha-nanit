# Legacy Installation (v0.x.x — Add-on + Integration)

> **This guide is for v0.x.x only.** If you're installing for the first time, use the current [README](README.md) instead. Version 1.x no longer requires the add-on.

## Prerequisites

- A Nanit account with email and password.
- Home Assistant OS or Supervised (for add-on support).
- HACS installed.

## Step 1: Install the Nanit Daemon Add-on

The v0.x.x integration requires the **Nanit Daemon (`nanitd`)** add-on — a Go-based service that handles WebSocket communication with the camera and exposes an HTTP API to the integration.

1. In Home Assistant, go to **Settings > Add-ons > Add-on Store**.
2. Click the three dots in the top-right corner and select **Repositories**.
3. Add the repository URL: `https://github.com/wealthystudent/ha-nanit`
4. Find **Nanit Daemon** in the store and install it.
5. Configure the add-on with your Nanit credentials (email, password).
6. Start the add-on.

## Step 2: Install the Integration

1. Open HACS and add `https://github.com/wealthystudent/ha-nanit` as a custom repository (category: **Integration**).
2. Search for **Nanit** and install **version 0.x.x** (not 1.x).
3. Restart Home Assistant.

## Step 3: Configure the Integration

1. Go to **Settings > Devices & Services > Add Integration**.
2. Search for **Nanit**.
3. The integration should auto-detect the running add-on. If not, enter the Go backend URL manually (usually `http://homeassistant.local:8080` or the add-on's hostname).
4. Complete the setup flow.

## Architecture (v0.x.x)

```
Home Assistant (Python)                  Nanit Daemon (Go)
custom_components/nanit/  ◄── HTTP ──►  nanitd add-on
                                            │
                                            ├── WebSocket ──► Nanit Camera (local)
                                            └── HTTPS     ──► Nanit Cloud (api.nanit.com)
```

In v0.x.x, the Go daemon (`nanitd`) acted as a bridge between the HA integration and the Nanit camera/cloud. The integration communicated with the daemon over HTTP.

## Upgrading to v1.x

See the [migration guide](README.md#migrating-from-v0x) in the main README.
