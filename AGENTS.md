# ha-nanit — AGENTS.md

This file is a quick lookup for agents working in this repo. Read it before making changes.

## Repo overview

Home Assistant custom integration for Nanit baby cameras.
Three main parts:

- **Python integration**: `custom_components/nanit/`
- **aionanit client library**: `packages/aionanit/` (pure-Python async Nanit API client)
- **Go add-on (legacy, deprecated)**: `nanitd/` — no longer used by the integration as of v1.0

## Architecture (high level)

```
Home Assistant (Python)
custom_components/nanit/
         │
         ├── aionanit (PyPI package)
         │      ├── WebSocket (wss) ──► Nanit Camera (local:442, self-signed TLS)
         │      ├── WebSocket (wss) ──► Nanit Cloud  (api.nanit.com/focus/*)
         │      └── HTTPS (REST)   ──► Nanit Cloud  (api.nanit.com)
         │
         └── HA Stream integration ──► rtmps://media-secured.nanit.com (live video)
```

### Data flow

- **Push-based sensors**: Camera → WebSocket → `NanitCamera.subscribe()` → `NanitPushCoordinator.async_set_updated_data()` → entities
- **Cloud events (motion/sound)**: `NanitCloudCoordinator` polls `GET /babies/{uid}/messages` every 30s
- **Camera stream**: `camera.stream_source()` returns an RTMPS URL with a fresh access token
- **Commands** (night light, volume, etc.): Entity → `NanitCamera.async_set_settings()` / `async_set_control()` → WebSocket → camera

## Key paths

### Integration (`custom_components/nanit/`)

- `manifest.json` — integration metadata + version
- `__init__.py` — `async_setup_entry` / `async_unload_entry`, `NanitData` dataclass
- `hub.py` — `NanitHub` lifecycle management (wraps `NanitClient` + `NanitCamera`)
- `config_flow.py` — UI setup + reauth/reconfigure (no add-on detection)
- `coordinator.py` — `NanitPushCoordinator` (WebSocket push) + `NanitCloudCoordinator` (polling)
- `entity.py` — `NanitEntity` base class with Shelly-style availability
- `camera.py`, `sensor.py`, `binary_sensor.py`, `switch.py`, `number.py` — entity platforms
- `diagnostics.py` — redacted debug output
- `strings.json` + `translations/en.json` — user-facing strings

### Client library (`packages/aionanit/`)

- `aionanit/__init__.py` — public API exports
- `aionanit/auth.py` — `TokenManager` (automatic refresh, callback on token change)
- `aionanit/rest.py` — `NanitRestClient` (login, MFA, babies, events, snapshots)
- `aionanit/camera.py` — `NanitCamera` (state machine, subscribe, commands)
- `aionanit/client.py` — `NanitClient` (top-level entrypoint, camera factory)
- `aionanit/models.py` — `CameraState`, `CameraEvent`, `Baby`, `CloudEvent`, etc.
- `aionanit/proto/nanit_pb2.py` — google protobuf generated types (via `protoc`)
- `aionanit/ws/transport.py` — `WsTransport` (WebSocket connection, reconnect, keepalive)
- `aionanit/ws/protocol.py` — protobuf encode/decode
- `aionanit/ws/pending.py` — `PendingRequests` (request/response correlation)
- `tests/` — 135 unit tests (pytest, aioresponses)

### Legacy (deprecated)

- `nanitd/` — Go daemon source (no longer used by integration)
- `.github/workflows/build-addon.yaml` — add-on build pipeline (legacy)

### Other

- `IMPLEMENTATION_PLAN.md` — detailed v1.0 rewrite plan (reference doc)
- `CHANGELOG.md` — release history
- `justfile` — release helper (bumps versions + creates tag)
- `hacs.json` — HACS custom integration config
- `README.md` — user-facing docs

## Development guidelines (Home Assistant)

- **Always use the latest Home Assistant version, API, and developer docs**
  from https://developers.home-assistant.io/.
- Minimum supported HA version: **2025.12+** (per README).
- Keep integration **fully async**; no blocking I/O in the event loop.
- Use `ConfigEntry.runtime_data` for runtime objects (clients/coordinators).
- Push-based coordinator: use `DataUpdateCoordinator.async_set_updated_data()` for WebSocket push data.
- Polling coordinator: use `DataUpdateCoordinator` with `update_interval` for cloud events.
- Avoid hardcoded English strings; use `strings.json`/translations.
- **Do not change entity unique IDs or class names** without a migration plan.
- Never log or store credentials/tokens unredacted (see `diagnostics.py`).

## Development guidelines (aionanit)

- All I/O must be async (`aiohttp`, `asyncio`).
- Use the shared `aiohttp.ClientSession` passed to `NanitClient` (do not create your own).
- Protobuf types are generated from `proto/nanit.proto` via `scripts/generate_proto.py`.
- WebSocket keepalive: ping every 25s, read deadline 60s.
- Local camera connections use self-signed TLS (`ssl.CERT_NONE`).
- Maximum 1 WebSocket connection per camera (local port 442 limit is 2, but we use 1).
- Run tests: `cd packages/aionanit && pytest`

## Commits & releases

- **One task/feature per commit**, with a compact description.
- If behavior or user-facing functionality changes, **update `README.md`**.
- **Release when impact is significant** (new features, breaking changes,
  substantial behavior changes). Minor internal changes can remain unreleased.

### Versioning

Version is stored in `custom_components/nanit/manifest.json` → `"version"`.

Use the helper in `justfile`:

```
just release patch
just release minor
just release major
```

## Verification (required)

- **Verify features work** in a Home Assistant instance.
- Run aionanit tests:

```
cd packages/aionanit
pytest
```

- If new tests or linters are added, run them as part of the change.

## CI / automation

- Add-on builds (legacy) are handled by `.github/workflows/build-addon.yaml`.

## Questions before changes

- If anything is unclear, **address questions before work begins**. Do not guess.
