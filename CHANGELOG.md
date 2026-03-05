# Changelog

All notable changes to the Nanit Home Assistant integration are documented in this file.


## [1.0.13] – 2026-03-05

### Fixed
- Fix connectivity binary sensor showing "Disconnected" — entity now reflects the actual HA-to-camera WebSocket connection state instead of the camera's self-reported cloud server status
- Fix proto2 default trap in status parser: unset `connection_to_server` field now returns `None` instead of `False`
- Connectivity entity stays available when camera disconnects so it can properly display the disconnected state

## [1.0.12] – 2026-03-05

### Fixed
- Fix switch commands timing out after idle periods (Nanit cloud relay silently expires WebSocket sessions; the keepalive only validated TCP liveness, not application session health)
- Force immediate token refresh on restore to prevent stale access token use after HA restarts

### Added
- Stale connection detection: reconnects automatically when idle > 5 minutes before sending commands
- Retry-after-reconnect: commands that fail due to transport errors or timeout are retried once after an inline reconnect
- Session health check loop: periodic `GET_STATUS` every 4.5 minutes prevents session staleness from building up
- Idle tracking on WsTransport (`idle_seconds` property)
- 12 new unit tests covering stale detection, retry logic, health check lifecycle, and token expiry

## [1.0.11] – 2026-03-03

### Fixed
- Restore switch state across HA restarts using `RestoreEntity` mixin (switches no longer show "off" when the camera is actually on)
- Fix missing camera snapshot thumbnail in sidebar (caused by `is_on` returning `False` before initial data arrived, which made the camera proxy return 503)
## [1.0.10] – 2026-03-03

### Fixed
- Restore missing `unique_id` assignment in camera entity (regression in v1.0.9 caused entity to disappear)

## [1.0.9] – 2026-03-03

### Fixed
- Fix camera stream not reloading after toggling camera power: use optimistic state merge when camera PUT response omits the settings/control sub-message
- Invalidate HA's cached stream object on camera sleep/wake transitions so a fresh RTMPS stream is established
- Clear stale stream in `async_turn_on`/`async_turn_off` to prevent dead stream reuse

### Added
- 5 new unit tests for optimistic state merge and subscriber notification

## [1.0.8] – 2026-03-03

### Fixed
- Fix double-toggle bug: use proto2 `HasField()` guards to distinguish unset scalar fields from default values (0/False) in settings and control parsers
- Defense-in-depth: only update camera state when PUT response actually echoes back the settings/control sub-message
- Restrict build-addon CI workflow to only trigger on legacy `v0.*` tags

### Added
- 10 new unit tests covering HasField parsing behavior and response guard logic

## [1.0.0] – 2026-02-28

### Breaking
- Complete rewrite: the Go add-on (`nanitd`) is **no longer required**. Remove it after upgrading.
- Existing config entries must be deleted and re-added (entity unique IDs are preserved).
- The `transport` configuration option has been removed. Local connectivity is now controlled by the optional camera IP field.
- Camera streaming changed from HLS (via Go proxy) to RTMPS (direct from Nanit cloud).

### Added
- `aionanit` — new pure-Python async client library for Nanit cameras (`packages/aionanit/`).
- Direct WebSocket communication with Nanit cameras (cloud and local).
- Protobuf-over-WebSocket protocol implementation using google `protobuf`.
- Push-based sensor updates via WebSocket (temperature, humidity, light, motion, sound).
- Automatic token refresh with proactive renewal before expiry.
- Local-first camera connectivity with automatic cloud fallback.
- Self-signed TLS support for local camera connections (port 442).
- Exponential backoff with jitter for WebSocket reconnection.
- 135 unit tests for the `aionanit` library.

### Changed
- Authentication now happens directly against the Nanit cloud API (no Go backend intermediary).
- All entity platforms rewritten to use `CameraState` dataclasses from `aionanit`.
- Coordinators rewritten: `NanitPushCoordinator` (WebSocket push) and `NanitCloudCoordinator` (event polling).
- Config flow simplified: removed add-on detection, Go backend URL, and transport mode selection.
- `iot_class` changed from `cloud_polling` to `cloud_push`.

### Removed
- Dependency on `nanitd` Go daemon / Home Assistant add-on.
- `api.py` (Go backend HTTP wrapper).
- Add-on auto-discovery and host resolution logic.
- HLS stream proxy (replaced by RTMPS).
- Transport mode selector (Local / Local + Cloud).

## [0.5.2] – 2026-02-26

### Added
- aarch64 (ARM64) architecture support for the add-on (Raspberry Pi, etc.)
- Standalone Docker deployment instructions for HA Container / Core users
- Environment variable reference table for `nanitd`
- Go Backend URL configuration option documentation

### Changed
- CI workflow now builds both `amd64` and `aarch64` images via matrix strategy
- Dockerfile supports cross-compilation for ARM64 targets
- README quick start and prerequisites updated for add-on vs standalone paths

## [0.5.0] – 2026-02-22

### Changed
 Use Home Assistant shared `aiohttp` session instead of creating a private one
 Correct `iot_class` from `local_polling` to `cloud_polling` in manifest
 Replace hardcoded English transport labels with `SelectSelector` and translation keys
 Standardise config keys to Home Assistant built-in constants (`CONF_EMAIL`, `CONF_PASSWORD`, `CONF_HOST`, `CONF_ACCESS_TOKEN`)
 Options flow now writes to `entry.options` instead of mutating `entry.data`
 Classify status-LED and mic-mute switches as `EntityCategory.CONFIG`
 Classify connectivity binary sensor as `EntityCategory.DIAGNOSTIC`
 Replace deprecated `asyncio.get_event_loop()` with `asyncio.get_running_loop()`
 Clean up sensor platform: use typed `NanitConfigEntry`, remove `UnitOfIlluminance` compat shim

### Added
 `quality_scale.yaml` for Home Assistant Integration Quality Scale tracking
 Brand submission to `home-assistant/brands` (PR #9783)

## [0.4.1] – 2026-02-22

### Fixed
 Preserve Unix timestamps in cloud activity events instead of converting to datetime strings

## [0.4.0] – 2026-02-22

### Added
 Cloud-based motion and sound binary sensors (replace activity event entity)
 API testing tools (`justfile` commands) for cloud event endpoints
 Verbose logging in Go daemon for easier debugging
 Integration icons

### Changed
 Replaced activity event entity with dedicated motion/sound binary sensors
 Updated documentation for cloud sensors and testing tools

## [0.3.2] – 2026-02-21

### Fixed
 Restore camera entity that was accidentally removed
 Smooth switch state transitions (optimistic updates)

## [0.3.1] – 2026-02-21

### Added
 Agent lookup documentation

### Changed
 Harden activity event parsing and device state management
 Update default enabled entities and documentation
 Confirm night light and power command behaviour

## [0.3.0] – 2026-02-20

### Changed
 Reworked camera on/off switch implementation

## [0.2.2] – 2026-02-20

### Added
 Camera on/off switch
 Snapshot thumbnail camera entity
 Activity event sensor
 Integration logo
 `justfile` for common development tasks

## [0.2.1] – 2026-02-20

### Changed
 Version bump (internal packaging fix)

## [0.2.0] – 2026-02-20

### Changed
 Version bump (internal packaging fix)

## [0.1.2] – 2026-02-20

### Changed
 Version bump (internal packaging fix)

## [0.1.1] – 2026-02-20

### Added
 Camera IP configuration option
 Entity icons
 Token provisioning flow with graceful startup
 CI workflow for add-on builds

### Fixed
 Camera stream URL resolution
 Event matching logic
 Entity default enabled states

### Changed
 Limit add-on builds to `aarch64` and `amd64` only
 Remove pre-built image reference so Supervisor builds add-on locally

## [0.1.0] – 2026-02-20

### Added
 Initial release of Nanit Home Assistant custom integration
 Nanit authentication with email/password and MFA support
 `nanitd` Go add-on for local camera communication
 Add-on auto-discovery and dynamic host resolution
 Camera live stream via WebRTC/RTSP
 Environment sensors (temperature, humidity, light)
 Night light switch
 Mic mute switch
 Status LED switch
 Baby connectivity binary sensor
