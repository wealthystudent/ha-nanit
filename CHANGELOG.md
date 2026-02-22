# Changelog

All notable changes to the Nanit Home Assistant integration are documented in this file.

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
