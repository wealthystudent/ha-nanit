"""Hub for the Nanit integration — owns NanitClient lifecycle.

One hub per config entry (one Nanit account). The hub discovers all
devices on the account (cameras and Sound & Light Machines) and creates
coordinators for each. A baby can have a camera, a speaker, or both —
setup handles every combination, including speaker-only accounts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.const import CONF_ACCESS_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from aionanit.exceptions import (
    NanitAuthError,
    NanitCameraUnavailable,
    NanitConnectionError,
)
from aionanit.models import Baby

from .aionanit_sl.sound_light import NanitSoundLight
from .const import CONF_CAMERA_IPS, CONF_REFRESH_TOKEN, CONF_SPEAKER_IPS, DOMAIN
from .coordinator import (
    NanitCloudCoordinator,
    NanitNetworkCoordinator,
    NanitPushCoordinator,
    NanitSoundLightCoordinator,
)
from .sanitize import display_name
from .sl_discovery import make_local_host_resolver

if TYPE_CHECKING:
    from aionanit import NanitCamera, NanitClient

    from . import NanitConfigEntry

_LOGGER = logging.getLogger(__name__)

# Maximum seconds to wait for a single camera to connect and initialise
# during hub setup.  Prevents an unreachable camera (e.g. a travel camera
# that is powered off) from blocking the entire integration indefinitely.
_CAMERA_SETUP_TIMEOUT: float = 60.0

# Same idea for a Sound & Light Machine. Its async_start returns once the
# transports are launched (connects continue in the background), so this
# is a safety net rather than an expected wait.
_SPEAKER_SETUP_TIMEOUT: float = 60.0


@dataclass
class CameraData:
    """Runtime data for a single camera within the account."""

    camera: NanitCamera
    baby: Baby
    push_coordinator: NanitPushCoordinator
    cloud_coordinator: NanitCloudCoordinator | None
    network_coordinator: NanitNetworkCoordinator | None = None


@dataclass
class SpeakerData:
    """Runtime data for a single Sound & Light Machine within the account."""

    sound_light: NanitSoundLight
    baby: Baby
    speaker_uid: str
    coordinator: NanitSoundLightCoordinator


class NanitHub:
    """Manages the NanitClient, token persistence, and all device instances.

    One hub per config entry (one Nanit account). Discovers all
    babies on the account and creates coordinators for each camera
    and each Sound & Light Machine.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        entry: NanitConfigEntry,
    ) -> None:
        """Initialize the hub with an existing session and config entry."""
        from aionanit.client import NanitClient

        self._hass = hass
        self._entry = entry
        self._client = NanitClient(session)
        self._camera_data: dict[str, CameraData] = {}
        self._speaker_data: dict[str, SpeakerData] = {}
        self._babies: list[Baby] = []
        self._failed_camera_uids: set[str] = set()
        self._speaker_uid_map: dict[str, str] = {}
        self._sound_lights: dict[str, NanitSoundLight] = {}
        self._unsubscribe_tokens: Callable[[], None] | None = None

    @property
    def client(self) -> NanitClient:
        """Return the underlying NanitClient."""
        return self._client

    @property
    def camera_data(self) -> dict[str, CameraData]:
        """Return per-camera runtime data, keyed by camera_uid."""
        return self._camera_data

    @property
    def speaker_data(self) -> dict[str, SpeakerData]:
        """Return per-speaker runtime data, keyed by speaker_uid."""
        return self._speaker_data

    @property
    def babies(self) -> list[Baby]:
        """Return all discovered babies (including ones that failed to connect)."""
        return self._babies

    @property
    def failed_camera_uids(self) -> set[str]:
        """Return UIDs of cameras that failed to connect during setup."""
        return self._failed_camera_uids

    @property
    def speaker_uid_map(self) -> dict[str, str]:
        """Return the resolved baby_uid → speaker_uid map."""
        return self._speaker_uid_map

    async def async_setup(self) -> None:
        """Restore tokens, discover devices, create coordinators for each.

        Sets up whatever devices exist on the account: camera only,
        Sound & Light only, or both per baby.

        Raises:
            NanitAuthError: If tokens are invalid (triggers reauth).
            NanitConnectionError: If no device on the account could be set up.

        """
        # Restore tokens from persisted config entry data
        access_token = self._entry.data[CONF_ACCESS_TOKEN]
        refresh_token = self._entry.data[CONF_REFRESH_TOKEN]
        self._client.restore_tokens(access_token, refresh_token)

        # Register callback to persist refreshed tokens
        tm = self._client.token_manager
        if tm is not None:
            self._unsubscribe_tokens = tm.on_tokens_refreshed(self._on_tokens_refreshed)

        # Fetch babies (also validates tokens)
        babies = await self._async_get_babies_tolerant()

        self._babies = list(babies)

        if not babies:
            _LOGGER.warning("No babies found on Nanit account")
            return

        speaker_uid_map = await self._async_resolve_speaker_uids(babies)
        self._speaker_uid_map = speaker_uid_map

        # Persist the resolved speaker UIDs so they survive restarts even if
        # a later /babies response omits them.
        stored_map = self._entry.data.get("speaker_uid_map", {})
        if speaker_uid_map and speaker_uid_map != stored_map:
            self._hass.config_entries.async_update_entry(
                self._entry,
                data={**self._entry.data, "speaker_uid_map": speaker_uid_map},
            )
            _LOGGER.debug("Persisted speaker UID map for %d camera(s)", len(speaker_uid_map))

        # Per-device IP configuration from options
        camera_ips: dict[str, str] = self._entry.options.get(CONF_CAMERA_IPS, {})
        speaker_ips: dict[str, str] = self._entry.options.get(CONF_SPEAKER_IPS, {})

        # Create coordinators for every device on the account
        tasks: list[Coroutine[Any, Any, None]] = []
        owners: list[tuple[str, Baby]] = []
        for baby in babies:
            if baby.camera_uid:
                tasks.append(
                    asyncio.wait_for(
                        self._setup_camera(baby, camera_ips.get(baby.camera_uid)),
                        timeout=_CAMERA_SETUP_TIMEOUT,
                    )
                )
                owners.append(("camera", baby))
            speaker_uid = speaker_uid_map.get(baby.uid)
            if speaker_uid:
                # Manual speaker IP, keyed by speaker_uid (legacy entries were
                # keyed by camera_uid; the setup-time migration re-keys them,
                # this fallback covers the first boot after upgrade).
                speaker_ip = speaker_ips.get(speaker_uid)
                if speaker_ip is None and baby.camera_uid:
                    speaker_ip = speaker_ips.get(baby.camera_uid)
                tasks.append(
                    asyncio.wait_for(
                        self._setup_speaker(baby, speaker_uid, speaker_ip),
                        timeout=_SPEAKER_SETUP_TIMEOUT,
                    )
                )
                owners.append(("speaker", baby))

        if not tasks:
            _LOGGER.warning("No cameras or Sound & Light Machines found on Nanit account")
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)

        failed_cameras: list[str] = []
        failed_speakers: list[str] = []
        for (kind, baby), result in zip(owners, results, strict=True):
            if isinstance(result, NanitAuthError):
                raise result
            if kind == "camera":
                if isinstance(result, NanitConnectionError | NanitCameraUnavailable | TimeoutError):
                    # getattr: published aionanit 1.8.7 wheels predate camera_connected.
                    cloud_connected = getattr(baby, "camera_connected", None)
                    cloud_status = (
                        "cloud reports connected=True (transient failure?)"
                        if cloud_connected is True
                        else "cloud reports connected=False (camera offline)"
                        if cloud_connected is False
                        else "cloud connected status unknown"
                    )
                    _LOGGER.warning(
                        "Camera %s (%s) failed to connect: %s — %s",
                        baby.name,
                        baby.camera_uid,
                        result,
                        cloud_status,
                    )
                    failed_cameras.append(display_name(baby.name, baby.uid))
                    self._failed_camera_uids.add(baby.camera_uid)
                    ir.async_create_issue(
                        self._hass,
                        DOMAIN,
                        f"camera_connection_failed_{baby.camera_uid}",
                        is_fixable=False,
                        is_persistent=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key="camera_connection_failed",
                        translation_placeholders={
                            "camera_name": display_name(baby.name, baby.uid),
                            "error": str(result),
                        },
                    )
                elif isinstance(result, BaseException):
                    raise result
            elif isinstance(result, Exception):
                # Speaker failures are non-fatal on their own: the S&L
                # transport keeps reconnecting in the background, and a
                # camera on the same account should not be blocked by it.
                _LOGGER.warning(
                    "Sound & Light Machine for %s failed to start; "
                    "sound/light entities disabled: %s",
                    baby.name,
                    result,
                )
                failed_speakers.append(display_name(baby.name, baby.uid))
            elif isinstance(result, BaseException):
                raise result

        if not self._camera_data and not self._speaker_data:
            raise NanitConnectionError(
                f"No devices could be set up: {', '.join(failed_cameras + failed_speakers)}"
            )

        if failed_cameras:
            _LOGGER.warning(
                "Some cameras failed to connect: %s. They will be retried on next reload.",
                ", ".join(failed_cameras),
            )

    async def _async_get_babies_tolerant(self) -> list[Baby]:
        """Fetch babies, tolerating rows without a camera.

        aionanit's Baby model requires camera_uid and its parser raises
        KeyError on an account whose baby has no camera paired. Until the
        library makes the field optional, fall back to parsing the raw
        /babies response with camera_uid defaulted to "" (treated as
        "no camera" throughout the hub).
        """
        try:
            return await self._client.async_get_babies()
        except KeyError:
            _LOGGER.debug(
                "aionanit could not parse /babies (baby without camera?); "
                "falling back to raw parse",
                exc_info=True,
            )
            return await self._async_get_babies_raw()

    async def _async_get_babies_raw(self) -> list[Baby]:
        """Parse /babies directly, allowing camera-less babies."""
        from aionanit.rest import (
            NANIT_API_HEADERS,
            _parse_camera_connected,
            _parse_camera_last_seen,
            _parse_network,
            _sanitize_name,
        )

        tm = self._client.token_manager
        if tm is None:
            raise NanitAuthError("Not authenticated — call async_login first")
        access_token = await tm.async_get_access_token()
        rest = self._client.rest_client
        try:
            resp = await rest.session.get(
                f"{rest.base_url}/babies",
                headers={**NANIT_API_HEADERS, "Authorization": access_token},
            )
        except aiohttp.ClientError as err:
            raise NanitConnectionError(str(err)) from err
        if resp.status == 401:
            raise NanitAuthError("Access token invalid")
        resp.raise_for_status()
        body = await resp.json()

        return [
            Baby(
                uid=baby["uid"],
                name=_sanitize_name(baby.get("name")),
                camera_uid=baby.get("camera_uid") or "",
                speaker_uid=((baby.get("speaker") or {}).get("speaker") or {}).get("uid"),
                network=_parse_network(baby),
                camera_connected=_parse_camera_connected(baby),
                camera_last_seen=_parse_camera_last_seen(baby),
            )
            for baby in body.get("babies", [])
        ]

    async def _async_resolve_speaker_uids(self, babies: list[Baby]) -> dict[str, str]:
        """Resolve each baby's speaker uid, keyed by baby uid.

        Sources, in order: the live /babies response (authoritative), the
        map persisted from an earlier run (also accepts the legacy
        camera_uid-keyed shape), then the raw /babies API as final fallback
        for an installed aionanit that predates the speaker_uid field.
        """
        speaker_uid_map: dict[str, str] = {}
        for baby in babies:
            uid = getattr(baby, "speaker_uid", None)
            if uid:
                speaker_uid_map[baby.uid] = uid

        stored: dict[str, str] = dict(self._entry.data.get("speaker_uid_map", {}))
        if stored:
            baby_uids = {baby.uid for baby in babies}
            by_camera = {baby.camera_uid: baby.uid for baby in babies if baby.camera_uid}
            for key, uid in stored.items():
                if key in baby_uids:
                    speaker_uid_map.setdefault(key, uid)
                elif key in by_camera:
                    speaker_uid_map.setdefault(by_camera[key], uid)

        if not speaker_uid_map:
            try:
                speaker_uid_map = await self._discover_speaker_uids()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Speaker UID discovery from raw API failed", exc_info=True)

        return speaker_uid_map

    async def _setup_camera(
        self,
        baby: Baby,
        camera_ip: str | None,
    ) -> None:
        """Create a camera instance and its coordinators for a single baby."""
        camera = self._client.camera(
            uid=baby.camera_uid,
            baby_uid=baby.uid,
            prefer_local=camera_ip is not None,
            local_ip=camera_ip,
        )

        push_coordinator = NanitPushCoordinator(self._hass, self._entry, camera, baby)
        await push_coordinator.async_setup()

        cloud_coordinator: NanitCloudCoordinator | None = None
        try:
            cloud_coordinator = NanitCloudCoordinator(self._hass, self._entry, self, baby)
            await cloud_coordinator.async_config_entry_first_refresh()
        except NanitAuthError:
            raise
        except NanitConnectionError:
            _LOGGER.warning(
                "Cloud coordinator for %s failed to start; cloud sensors disabled",
                baby.name,
            )
            cloud_coordinator = None

        ir.async_delete_issue(self._hass, DOMAIN, f"camera_connection_failed_{baby.camera_uid}")

        # Network diagnostics coordinator (optional — polls GET /babies for WiFi info)
        network_coordinator: NanitNetworkCoordinator | None = None
        try:
            network_coordinator = NanitNetworkCoordinator(self._hass, self._entry, self, baby)
            await network_coordinator.async_config_entry_first_refresh()
        except NanitAuthError:
            raise
        except NanitConnectionError:
            _LOGGER.debug(
                "Network coordinator for %s failed to start; network sensors disabled",
                baby.name,
            )
            network_coordinator = None

        self._camera_data[baby.camera_uid] = CameraData(
            camera=camera,
            baby=baby,
            push_coordinator=push_coordinator,
            cloud_coordinator=cloud_coordinator,
            network_coordinator=network_coordinator,
        )

    async def _setup_speaker(
        self,
        baby: Baby,
        speaker_uid: str,
        speaker_ip: str | None,
    ) -> None:
        """Create a Sound & Light instance and its coordinator for a single baby."""
        sound_light = self.get_sound_light(speaker_uid, speaker_ip)
        coordinator = NanitSoundLightCoordinator(
            self._hass,
            self._entry,
            sound_light,
            baby,
            via_camera_uid=baby.camera_uid or None,
        )
        await coordinator.async_setup()

        self._speaker_data[speaker_uid] = SpeakerData(
            sound_light=sound_light,
            baby=baby,
            speaker_uid=speaker_uid,
            coordinator=coordinator,
        )

    @callback
    def _on_tokens_refreshed(self, new_access: str, new_refresh: str) -> None:
        """Persist refreshed tokens to the config entry."""
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                CONF_ACCESS_TOKEN: new_access,
                CONF_REFRESH_TOKEN: new_refresh,
            },
        )

    def get_sound_light(
        self,
        speaker_uid: str,
        device_ip: str | None = None,
    ) -> NanitSoundLight:
        """Get or create a NanitSoundLight instance."""
        if speaker_uid in self._sound_lights:
            return self._sound_lights[speaker_uid]

        if self._client.token_manager is None:
            raise NanitAuthError("Not authenticated — call async_login first")

        sl = NanitSoundLight(
            speaker_uid=speaker_uid,
            token_manager=self._client.token_manager,
            rest_client=self._client.rest_client,
            session=self._client.session,
            device_ip=device_ip,
            # LAN discovery via HA's zeroconf. The configured speaker IP
            # (device_ip) takes precedence as a manual override; discovery
            # covers everyone else, best-effort with cloud-relay fallback.
            local_host_resolver=make_local_host_resolver(self._hass),
        )
        self._sound_lights[speaker_uid] = sl
        return sl

    async def async_close(self) -> None:
        """Stop all devices and clean up."""
        if self._unsubscribe_tokens is not None:
            self._unsubscribe_tokens()
            self._unsubscribe_tokens = None
        await self._client.async_close()
        self._camera_data.clear()
        self._speaker_data.clear()
        # Also stop S&L instances
        for sl in list(self._sound_lights.values()):
            try:
                await sl.async_stop()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Error stopping S&L during close")
        self._sound_lights.clear()

    async def _discover_speaker_uids(self) -> dict[str, str]:
        """Fetch speaker UIDs from the raw /babies API response, keyed by baby uid.

        Fallback for when the installed aionanit package's Baby dataclass
        does not yet include the speaker_uid field.
        """
        tm = self._client.token_manager
        if tm is None:
            return {}
        access_token = await tm.async_get_access_token()
        rest = self._client.rest_client
        async with rest.session.get(
            f"{rest.base_url}/babies",
            headers={"Authorization": access_token},
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()

        result: dict[str, str] = {}
        for baby in body.get("babies", []):
            baby_uid = baby.get("uid")
            speaker_data = baby.get("speaker", {})
            speaker_obj = speaker_data.get("speaker", {})
            speaker_uid = speaker_obj.get("uid")
            if baby_uid and speaker_uid:
                result[baby_uid] = speaker_uid
                _LOGGER.debug("Discovered speaker UID %s for baby %s", speaker_uid, baby_uid)
        return result
