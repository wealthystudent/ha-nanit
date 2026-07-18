# Connection Reliability

Three mechanisms ensure entities never go unavailable and commands never fail due to token expiry or transient disconnections.

## Pre-emptive token refresh (Fix A — `NanitCamera._token_refresh_loop`)

Nanit access tokens expire after 3600s (1 hour). Instead of waiting for the server to close the WebSocket, a background task in `NanitCamera` calculates the time remaining until token expiry and forces a reconnect ~5 minutes before. The transport's reconnect loop fetches fresh headers (via `_get_headers` → `TokenManager.async_get_access_token`), so the new connection uses a valid token. This eliminates server-initiated disconnects entirely.

## Availability grace period (Fix B — `NanitPushCoordinator`)

When the WebSocket disconnects, the coordinator does NOT immediately mark entities as unavailable. Instead, it starts a 30-second timer (`_AVAILABILITY_GRACE_SECONDS`). If the connection recovers within the grace period (which it should in 2-4 seconds), the timer is cancelled and entities were never marked unavailable. Only if the grace period expires with no reconnection does `connected` flip to `False`.

## Command wait-for-connection (Fix C — `NanitCamera._connected_event`)

An `asyncio.Event` tracks the connection state. When a command (`_send_request`) is called while the transport is disconnected, it waits up to 15 seconds for the event to be set (indicating reconnection completed). If reconnection happens within the window, the command proceeds normally. If the timeout expires, it falls through to the existing inline reconnect + retry logic.

## Summary

**Together**: A prevents most disconnects. B hides the brief ones that do happen. C ensures commands survive them.

## Sound & Light Machine

The S&L transport (`custom_components/nanit/aionanit_sl/`) was ported from
[nanit-sound-light](https://github.com/com6056/nanit-sound-light), where each
behavior was reverse-engineered from the official app and validated against a
real speaker. Its reliability model is deliberately different from the camera
path, because the speaker reacts badly to patterns that are fine for the
camera:

- **One request in flight, await-ack, never re-send.** Every request carries
  a unique id and the sender waits (10s) for the matching response before the
  next send. A slow ack means the device is busy, not gone. Re-sending piles
  duplicate commands onto it until it stops responding for ~30 seconds, so a
  slow ack is accepted optimistically and never retried (the official app
  never retries either). Only a socket drop or an explicit non-2xx rejection
  fails a command, which rolls back the optimistic entity state.
- **Backend readiness gate.** On a fresh relay connect, the device's first
  frame reports whether the physical speaker is attached behind the relay.
  Nothing is sent until it is, because commands into a detached relay only
  stall. Attachment is sticky: the speaker emits bare or Disconnected backend
  frames periodically while fully usable, and those must not detach it. Only
  a socket drop does.
- **Command coalescing and the pin window.** Commands arriving within 150ms
  merge into one combined `Settings` write, so a scene touching power, sound,
  volume, and light sends one message instead of four racing ones. Commanded
  fields are pinned for up to 30s so the device's lagging state reports can't
  flip a just-issued value back. A pin releases as soon as the device
  confirms the value.
- **Dual sockets, prefer local.** The cloud relay and a direct LAN socket
  (mDNS-discovered address, trust-all TLS on port 442, per-speaker device
  token auth) can both be open at once. Sends prefer local. Reconnects use
  the app's own backoff schedules (remote 0/2/5/7s, local 0/3/10/60/90s), and
  a transport whose handshake keeps getting auth-rejected backs off to a long
  quiet interval so a wedged speaker can't flood the log.
- **The speaker accepts only ONE local client at a time** (verified on real
  hardware, firmware 1.3.1: a second client's local handshake gets HTTP 403
  even with a freshly minted device token, and connects the moment the first
  client releases the socket). A second HA instance, or another integration
  holding the local socket, therefore runs on the cloud relay. The transport
  keeps retrying with backoff and takes the local slot automatically when it
  frees. This also means a persistent local 403 does not necessarily mean the
  speaker is wedged: something else may simply own the slot.
- **Keepalive is WebSocket protocol ping (~20s) only.** The speaker has no
  app-level keepalive frame. Do not add one (the camera's 25s keepalive
  message is a camera-only pattern).
- **Availability** requires a live socket AND attachment, debounced by the
  coordinator's 30-second grace period (the same pattern as Fix B above).

## Related files

- `packages/aionanit/aionanit/camera.py` — `_token_refresh_loop`, `_connected_event`, `_send_request`
- `packages/aionanit/aionanit/ws/transport.py` — `WsTransport` reconnect loop
- `custom_components/nanit/coordinator.py` — `NanitPushCoordinator` availability grace period, `NanitSoundLightCoordinator` grace period
- `custom_components/nanit/aionanit_sl/transport.py` (S&L sockets, transaction model, readiness gate, backoff)
- `custom_components/nanit/aionanit_sl/sound_light.py` (S&L command coalescing, pin window, optimistic rollback)
