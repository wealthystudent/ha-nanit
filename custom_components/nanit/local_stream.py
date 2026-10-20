"""Local streaming: the camera pushes to Home Assistant instead of the Nanit cloud.

One receiver serves every Nanit config entry on this Home Assistant (a second
account would otherwise fail to bind the port). Each entry gets a
``LocalStreamEntry`` that owns its cameras' stream keys, the failure strikes
and the pause, and each camera a ``LocalStreamCamera`` with its mode.

A camera is in ``local`` mode unless local streaming is paused or the camera
is in a cooldown after a miss, in which case it is in ``cloud`` mode and
Home Assistant uses today's cloud stream. The mode is per camera, never per
consumer: the camera has one push target for all of Home Assistant, so HLS
and WebRTC must follow the same mode or they would fight over the push.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from homeassistant.components.network import async_get_source_ip
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.hassio import is_hassio
from homeassistant.util.hass_dict import HassKey
from homeassistant.util.package import is_docker_env

from aionanit.local_stream import LocalStreamServer, StreamChannel

from .const import (
    CONF_LOCAL_STREAM,
    CONF_LOCAL_STREAM_HOST,
    CONF_LOCAL_STREAM_PORT,
    DEFAULT_LOCAL_STREAM_PORT,
    DOMAIN,
    local_stream_default,
)

# The routing table's own answer for one target is a Home Assistant internal.
# If a release moves or renames it, only the advertised address loses it,
# rather than the whole integration failing to import.
try:
    from homeassistant.components.network.util import async_get_source_ip as _ha_routed_ip
except ImportError:  # pragma: no cover - depends on the Home Assistant version
    _ha_routed_ip = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

DATA_KEY: HassKey[_Shared] = HassKey(f"{DOMAIN}_local_stream")
LOCK_KEY: HassKey[asyncio.Lock] = HassKey(f"{DOMAIN}_local_stream_lock")
ENTRY_DATA_KEY = "local_stream_state"

PORT_ATTEMPTS = 5
COOLDOWN = 600.0
# Consecutive misses of any kind double the cooldown, up to this. A miss that
# is neither a strike nor a protocol failure would otherwise force a retry,
# and a blip for anyone watching, every ten minutes forever.
MAX_COOLDOWN = 6 * 3600.0
STRIKES_TO_PAUSE = 2
PROTOCOL_FAILURES_TO_PAUSE = 3
PAUSE_RETRY_AFTER = 24 * 3600.0
ACK_TIMEOUT = 3.0
# The camera pushed within 1-2 s in testing. The wait stays inside the
# receiver's 15 s pre-publish window, so a slow camera is still caught here
# instead of publishing just after a miss was recorded.
PUBLISH_WAIT = 12.0
# After a "stream limit" answer the camera may still be pushing to this URL
# (it reconnects to a receiver that came back with the same key within a
# couple of seconds), so wait for it, but not the full window.
BUSY_PUBLISH_WAIT = 5.0

ISSUE_PORT_IN_USE = "local_stream_port_in_use"
ISSUE_UNREACHABLE = "local_stream_unreachable"
ISSUE_UNREACHABLE_DOCKER = "local_stream_unreachable_docker"
ISSUE_PROTOCOL = "local_stream_protocol_error"
LEARN_MORE_URL = "https://github.com/wealthystudent/ha-nanit#local-streaming"

_TLS_DIR = "nanit_local_stream"


def async_get_routed_ip(target: str) -> str | None:
    """Return the address the routing table uses to reach ``target``, if known."""
    if _ha_routed_ip is None:
        return None
    try:
        return _ha_routed_ip(target)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not look up the route to %s: %s", target, err)
        return None


def local_stream_enabled(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return whether local streaming is on for the entry."""
    value = entry.options.get(CONF_LOCAL_STREAM)
    if value is None:
        return local_stream_default(hass)
    return value is not False


# ----------------------------------------------------------------------
# TLS material, one self-signed cert per Home Assistant
# ----------------------------------------------------------------------


def _load_or_create_tls(directory: Path) -> ssl.SSLContext:
    """Build the server SSL context, generating the cert on first use.

    Runs in an executor: key generation, file I/O and load_cert_chain all
    block. The camera does not validate the certificate (tested on a Pro),
    so TLS here protects against passive sniffing on the LAN.
    """
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    if not (cert_path.is_file() and key_path.is_file()):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Home Assistant Nanit")])
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .sign(key, hashes.SHA256())
        )
        key_bytes = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as key_file:
            key_file.write(key_bytes)
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    return context


# ----------------------------------------------------------------------
# Shared receiver
# ----------------------------------------------------------------------


@dataclass
class _Shared:
    server: LocalStreamServer
    requested_port: int = DEFAULT_LOCAL_STREAM_PORT
    users: set[str] = field(default_factory=set)


def _lock(hass: HomeAssistant) -> asyncio.Lock:
    if (lock := hass.data.get(LOCK_KEY)) is None:
        lock = hass.data[LOCK_KEY] = asyncio.Lock()
    return lock


async def _async_acquire_server(hass: HomeAssistant, entry: ConfigEntry) -> LocalStreamServer:
    """Return the shared receiver, starting it for the first entry."""
    async with _lock(hass):
        shared = hass.data.get(DATA_KEY)
        if shared is None:
            directory = Path(hass.config.path(".storage", _TLS_DIR))
            context = await hass.async_add_executor_job(_load_or_create_tls, directory)
            port = int(entry.options.get(CONF_LOCAL_STREAM_PORT, DEFAULT_LOCAL_STREAM_PORT))
            explicit_host = bool(entry.options.get(CONF_LOCAL_STREAM_HOST))
            server = LocalStreamServer(
                port=port,
                ssl_context=context,
                # A set address implies a deliberate port mapping, so no
                # silent move to another port.
                port_attempts=1 if explicit_host else PORT_ATTEMPTS,
            )
            await server.async_start()
            shared = _Shared(server, requested_port=port)
            hass.data[DATA_KEY] = shared
        elif entry.options.get(CONF_LOCAL_STREAM_PORT, DEFAULT_LOCAL_STREAM_PORT) != (
            shared.requested_port
        ):
            _LOGGER.warning(
                "Nanit local streaming already listens on port %s for another account; "
                "this account's port option is ignored",
                shared.server.rtmp_port,
            )
        shared.users.add(entry.entry_id)
        return shared.server


async def _async_release_server(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the entry's claim, stopping the receiver after the last one."""
    async with _lock(hass):
        shared = hass.data.get(DATA_KEY)
        if shared is None:
            return
        shared.users.discard(entry_id)
        if not shared.users:
            hass.data.pop(DATA_KEY, None)
            await shared.server.async_stop()


# ----------------------------------------------------------------------
# Per entry
# ----------------------------------------------------------------------


class LocalStreamEntry:
    """Local streaming state for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, server: LocalStreamServer) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.server = server
        self.cameras: dict[str, LocalStreamCamera] = {}
        stored = entry.data.get(ENTRY_DATA_KEY)
        state = stored if isinstance(stored, dict) else {}
        keys = state.get("keys")
        self._keys: dict[str, str] = dict(keys) if isinstance(keys, dict) else {}
        strikes = state.get("strikes")
        self.strikes = int(strikes) if isinstance(strikes, int) else 0
        paused = state.get("paused_at")
        self.paused_at: float | None = float(paused) if isinstance(paused, int | float) else None
        reason = state.get("paused_reason")
        self.paused_reason: str | None = reason if isinstance(reason, str) and reason else None
        self.protocol_failures = 0
        self._cancel_pause_timer: Callable[[], None] | None = None
        self.unloaded = False
        host = state.get("host")
        self.last_host: str | None = host if isinstance(host, str) and host else None
        stored_fp = state.get("options_fp")
        self._stored_options_fp: str | None = stored_fp if isinstance(stored_fp, str) else None

    @property
    def paused(self) -> bool:
        """Return True while local streaming is paused (24 h retry)."""
        if self.paused_at is None:
            return False
        return time.time() - self.paused_at < PAUSE_RETRY_AFTER

    def _options_fp(self) -> str:
        options = self.entry.options
        relevant = {
            "port": options.get(CONF_LOCAL_STREAM_PORT, DEFAULT_LOCAL_STREAM_PORT),
            "host": options.get(CONF_LOCAL_STREAM_HOST, ""),
        }
        return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]

    def options_changed(self) -> bool:
        """Return True if the options that steer local streaming changed."""
        return self._stored_options_fp is not None and self._stored_options_fp != (
            self._options_fp()
        )

    @callback
    def clear_pause(self) -> None:
        """Forget strikes and the pause, and remove their issues."""
        self.strikes = 0
        self.protocol_failures = 0
        self.paused_at = None
        self.paused_reason = None
        self._persist()
        for issue in (ISSUE_UNREACHABLE, ISSUE_PROTOCOL):
            ir.async_delete_issue(self.hass, DOMAIN, f"{issue}_{self.entry.entry_id}")

    def prune_keys(self, camera_uids: set[str]) -> None:
        """Forget stream keys of cameras that left the account."""
        if set(self._keys) - camera_uids:
            self._keys = {uid: key for uid, key in self._keys.items() if uid in camera_uids}
            self._persist()

    def camera(self, camera_uid: str) -> LocalStreamCamera | None:
        """Register a camera with the receiver and return its handle.

        Returns None for a camera another account already streams locally
        (Nanit supports sharing a camera), which then uses the cloud stream:
        the camera has one push target per client, and both accounts share
        this Home Assistant.
        """
        if camera_uid not in self.cameras and self.server.channel(camera_uid) is not None:
            _LOGGER.info(
                "Camera %s is already streamed locally by another Nanit account here, "
                "so this account uses the cloud stream for it",
                camera_uid,
            )
            return None
        key = self._keys.get(camera_uid)
        if not key:
            key = secrets.token_urlsafe(16)
            self._keys[camera_uid] = key
            self._persist()
        channel = self.server.register(camera_uid, key)
        cam = LocalStreamCamera(self, camera_uid, channel)
        self.cameras[camera_uid] = cam
        return cam

    async def async_advertised_host(self) -> str:
        """Return the address the camera should push to."""
        host = str(self.entry.options.get(CONF_LOCAL_STREAM_HOST) or "").strip()
        if not host:
            target = None
            # learned from a previous publish
            for cam in self.cameras.values():
                if cam.channel.publisher_peer:
                    with contextlib.suppress(ValueError):
                        target = str(ipaddress.ip_address(cam.channel.publisher_peer))
                        break
            # The routing table's answer for the camera's address. HA's own
            # helper swaps in the first enabled adapter when the route goes
            # out another one (an IoT VLAN, say), which the camera can't reach.
            routed = async_get_routed_ip(target) if target else None
            host = routed or await async_get_source_ip(self.hass)
        self.last_host = host
        return host

    # Failure bookkeeping ------------------------------------------------

    @callback
    def record_success(self) -> None:
        """Clear strikes, the pause and the issues after a push arrived."""
        was_paused = self.paused_at is not None
        changed = self.strikes or was_paused or self.protocol_failures
        self.strikes = 0
        self.protocol_failures = 0
        self.paused_at = None
        self.paused_reason = None
        if changed:
            self._persist()
        for issue in (ISSUE_UNREACHABLE, ISSUE_PROTOCOL):
            ir.async_delete_issue(self.hass, DOMAIN, f"{issue}_{self.entry.entry_id}")
        if was_paused:
            # A push that arrived on its own (still flowing across a restart)
            # ended the pause, so hand the cameras back to local now.
            self.cancel_timers()
            for cam in self.cameras.values():
                cam.notify_mode_changed()

    @callback
    def record_strike(self, publish_url_host: str) -> None:
        """Count a clean miss: the camera acked but never connected."""
        self.strikes += 1
        self.protocol_failures = 0
        if self.strikes >= STRIKES_TO_PAUSE:
            self._pause(ISSUE_UNREACHABLE, publish_url_host)
        self._persist()

    @callback
    def record_protocol_failure(self) -> None:
        """Count a connection that arrived but failed TLS or RTMP."""
        self.protocol_failures += 1
        if self.protocol_failures >= PROTOCOL_FAILURES_TO_PAUSE:
            self._pause(ISSUE_PROTOCOL, self.last_host or "")
            self._persist()

    def _pause(self, issue: str, host: str) -> None:
        if host:
            self.last_host = host
        self.paused_at = time.time()
        self.paused_reason = issue
        self.schedule_pause_end()
        _LOGGER.warning(
            "Pausing Nanit local streaming for 24 hours (%s), using the cloud stream", issue
        )
        self.async_raise_issue(issue, host)

    @callback
    def async_raise_issue(self, issue: str, host: str) -> None:
        """Create (or refresh) the repair issue for the current pause."""
        translation_key = issue
        if issue == ISSUE_UNREACHABLE and self._looks_like_docker_bridge(host):
            translation_key = ISSUE_UNREACHABLE_DOCKER
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{issue}_{self.entry.entry_id}",
            is_fixable=True,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders={
                "address": f"{host}:{self.server.rtmp_port}",
                "port": str(self.server.rtmp_port),
            },
            learn_more_url=LEARN_MORE_URL,
            data={"entry_id": self.entry.entry_id},
        )

    def _looks_like_docker_bridge(self, host: str) -> bool:
        # A configured address is the user's choice (a second network, say),
        # not a detected container address.
        if str(self.entry.options.get(CONF_LOCAL_STREAM_HOST) or "").strip():
            return False
        # HA OS runs in a container too but has host networking.
        if not is_docker_env() or is_hassio(self.hass):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(
            address in ipaddress.ip_network(net) for net in ("172.16.0.0/12", "10.88.0.0/16")
        )

    @callback
    def schedule_pause_end(self) -> None:
        """Hand the cameras back to local streaming when the pause lapses."""
        if self._cancel_pause_timer is not None:
            self._cancel_pause_timer()
            self._cancel_pause_timer = None
        if self.paused_at is None or self.unloaded:
            return
        remaining = max(0.0, self.paused_at + PAUSE_RETRY_AFTER - time.time())
        self._cancel_pause_timer = async_call_later(self.hass, remaining, self._pause_ended)

    @callback
    def _pause_ended(self, _now: object = None) -> None:
        self._cancel_pause_timer = None
        if self.paused:
            # The loop's clock ran ahead of the wall clock. Try again later.
            self.schedule_pause_end()
            return
        for cam in self.cameras.values():
            cam.notify_mode_changed()

    @callback
    def cancel_timers(self) -> None:
        """Stop the pause timer."""
        if self._cancel_pause_timer is not None:
            self._cancel_pause_timer()
            self._cancel_pause_timer = None

    @callback
    def async_retry_now(self) -> None:
        """Clear the pause from the repair flow, without a reload."""
        self.cancel_timers()
        self.clear_pause()
        for cam in self.cameras.values():
            cam.misses = 0
            cam.end_cooldown()

    def _persist(self) -> None:
        fingerprint = self._options_fp()
        state: dict[str, object] = {
            "keys": dict(self._keys),
            "strikes": self.strikes,
            "paused_at": self.paused_at,
            "paused_reason": self.paused_reason,
            "host": self.last_host,
            "options_fp": fingerprint,
        }
        self._stored_options_fp = fingerprint
        if self.entry.data.get(ENTRY_DATA_KEY) == state:
            return
        # entry.data, never options: an options change reloads the entry.
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, ENTRY_DATA_KEY: state}
        )

    def diagnostics(self) -> dict[str, object]:
        """Return diagnostics (redacted by the caller)."""
        return {
            "rtmp_port": self.server.rtmp_port,
            "tls": self.server.tls,
            "advertised_host": self.last_host,
            "strikes": self.strikes,
            "paused": self.paused,
            "paused_reason": self.paused_reason,
            "connection_attempts": self.server.connection_attempts,
            "protocol_failures": self.server.protocol_failures,
            "cameras": [cam.diagnostics() for cam in self.cameras.values()],
        }


class LocalStreamCamera:
    """A camera's local stream mode and its channel on the receiver."""

    def __init__(
        self,
        owner: LocalStreamEntry,
        camera_uid: str,
        channel: StreamChannel,
    ) -> None:
        """Initialize."""
        self.owner = owner
        self.camera_uid = camera_uid
        self.channel = channel
        self.cooldown_until = 0.0
        self.fallbacks = 0
        self.misses = 0
        self.last_fallback: str | None = None
        # Set by the camera entity, called when local streaming is allowed
        # again from outside the entity (the repair's retry).
        self.on_mode_changed: Callable[[], None] | None = None

    @property
    def active(self) -> bool:
        """Return True when the camera should stream locally right now."""
        return not self.owner.paused and time.monotonic() >= self.cooldown_until

    def flv_url(self) -> str:
        """Return the loopback URL Home Assistant consumes."""
        return self.owner.server.flv_url(self.camera_uid)

    async def async_publish_url(self) -> tuple[str, str]:
        """Return the push URL for the camera and the host it names."""
        host = await self.owner.async_advertised_host()
        return self.owner.server.publish_url(host, self.camera_uid), host

    def start_cooldown(self, reason: str, *, count_miss: bool = True) -> None:
        """Use the cloud stream for a while after a miss, longer each time.

        ``count_miss=False`` is for a miss that won't repeat on the next try
        (the camera went to sleep), which doesn't grow the backoff.
        """
        if count_miss:
            self.misses += 1
        # A miss that won't repeat gets the base cooldown.
        seconds = self.cooldown_seconds() if count_miss else COOLDOWN
        self.cooldown_until = time.monotonic() + seconds
        self.fallbacks += 1
        self.last_fallback = reason

    def cooldown_seconds(self) -> float:
        """Return the cooldown for the current run of misses."""
        doublings = min(max(0, self.misses - 1), 16)
        return min(COOLDOWN * float(1 << doublings), MAX_COOLDOWN)

    def record_publish(self) -> None:
        """Reset the miss backoff after a push arrived."""
        self.misses = 0

    def end_cooldown(self) -> None:
        """Allow local streaming again right away."""
        self.cooldown_until = 0.0
        self.notify_mode_changed()

    def notify_mode_changed(self) -> None:
        """Tell the camera entity that local streaming may be allowed again."""
        if self.on_mode_changed is not None:
            self.on_mode_changed()

    def diagnostics(self) -> dict[str, object]:
        """Return per-camera diagnostics."""
        channel = self.channel
        return {
            "mode": "local" if self.active else "cloud",
            "publishing": channel.publishing,
            "publisher_peer": channel.publisher_peer,
            "last_publish_at": channel.last_publish_at,
            "publish_count": channel.publish_count,
            "bytes_in": channel.bytes_in,
            "consumers": channel.consumer_count,
            "fallbacks": self.fallbacks,
            "last_fallback": self.last_fallback,
        }


async def async_setup_local_stream(
    hass: HomeAssistant, entry: ConfigEntry
) -> LocalStreamEntry | None:
    """Start (or join) the receiver for an entry, or return None.

    A receiver that cannot bind raises a repair issue and leaves the entry
    on the cloud stream rather than failing setup.
    """
    if not local_stream_enabled(hass, entry):
        for issue in (ISSUE_PORT_IN_USE, ISSUE_UNREACHABLE, ISSUE_PROTOCOL):
            ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")
        state = entry.data.get(ENTRY_DATA_KEY)
        if isinstance(state, dict) and (state.get("paused_at") or state.get("strikes")):
            # Turning it back on later is a fresh start.
            cleared = {**state, "strikes": 0, "paused_at": None, "paused_reason": None}
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, ENTRY_DATA_KEY: cleared}
            )
        return None
    try:
        server = await _async_acquire_server(hass, entry)
    except OSError as err:
        port = entry.options.get(CONF_LOCAL_STREAM_PORT, DEFAULT_LOCAL_STREAM_PORT)
        _LOGGER.warning("Nanit local streaming could not listen on port %s: %s", port, err)
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{ISSUE_PORT_IN_USE}_{entry.entry_id}",
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PORT_IN_USE,
            translation_placeholders={"port": str(port), "error": str(err)},
            learn_more_url=LEARN_MORE_URL,
        )
        return None
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_PORT_IN_USE}_{entry.entry_id}")
    local = LocalStreamEntry(hass, entry, server)
    if local._stored_options_fp is None:
        local._persist()  # record the fingerprint for the next setup
    elif local.options_changed():
        # Any options change (port or address) is likely the fix
        # for whatever paused local streaming, so retry right away.
        local.clear_pause()
    if local.paused and local.paused_reason:
        local.async_raise_issue(local.paused_reason, local.last_host or "")
        local.schedule_pause_end()
    return local


async def async_unload_local_stream(hass: HomeAssistant, local: LocalStreamEntry) -> None:
    """Unregister the entry's cameras and release the receiver."""
    local.unloaded = True
    local.cancel_timers()
    for camera_uid in list(local.cameras):
        local.server.unregister(camera_uid)
    local.cameras.clear()
    await _async_release_server(hass, local.entry.entry_id)
