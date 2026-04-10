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

## Related files

- `packages/aionanit/aionanit/camera.py` — `_token_refresh_loop`, `_connected_event`, `_send_request`
- `packages/aionanit/aionanit/ws/transport.py` — `WsTransport` reconnect loop
- `custom_components/nanit/coordinator.py` — `NanitPushCoordinator` availability grace period
