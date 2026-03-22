# Multi-camera support

Tests that the integration correctly discovers and manages multiple cameras on one Nanit account.

**Related:** PR #12, Issue #9

## Prerequisites

- Dev HA running (`just dev`)
- Integration added with your Nanit credentials

## A. Multiple physical cameras

If you have multiple cameras on the same Nanit account, no code changes are needed. All cameras appear automatically after adding the integration.

### Verification

- [ ] All cameras appear as separate devices in Settings → Devices
- [ ] Each camera has its own entities (temperature, humidity, light, switches, stream)
- [ ] Entity unique IDs follow `{camera_uid}_{key}` pattern (check via Developer Tools → States)
- [ ] Per-camera IP config: Settings → Nanit → Configure → select camera → enter IP
- [ ] `just dev-restart` → all cameras reconnect
- [ ] Remove integration → all cameras + entities removed
- [ ] Re-add integration → all cameras reappear

## B. Single camera (simulated multi-camera)

Clone your real baby with a different UID so the hub discovers two cameras. Both connect to the same physical device over LAN.

### Code change

In `custom_components/nanit/hub.py`, method `async_setup()`, add after **line 96** (`babies = await self._client.async_get_babies()`):

```python
# --- DEV ONLY: clone camera for multi-device testing. Remove before committing. ---
from aionanit.models import Baby
babies.append(Baby(uid="clone_baby", name="Clone Camera", camera_uid="clone_cam"))
```

The full context around the change:

```python
    async def async_setup(self) -> None:
        ...
        # Fetch babies (also validates tokens)
        babies = await self._client.async_get_babies()

        # --- DEV ONLY: clone camera for multi-device testing. Remove before committing. ---
        from aionanit.models import Baby
        babies.append(Baby(uid="clone_baby", name="Clone Camera", camera_uid="clone_cam"))

        if not babies:
            ...
```

### Steps

1. Apply the code change above
2. `just dev-restart`
3. Go to Settings → Nanit → Configure
4. Select "Clone Camera" → enter your real camera's LAN IP (e.g. `192.168.1.x`)
5. HA reloads the integration — both cameras now connect to the same physical device

### What works on the clone

| Feature | Works? | Why |
|---------|--------|-----|
| Temperature, humidity, light sensors | Yes | Local WebSocket push |
| Night light switch | Yes | Local WebSocket command |
| Camera power switch | Yes | Local WebSocket command |
| Volume control | Yes | Local WebSocket command |
| Camera stream (RTMPS) | Yes | Stream URL from cloud API |
| Motion/sound detection | No | Cloud doesn't know `clone_cam` |
| Connectivity sensor | Yes | Tracks local WebSocket state |

### Verification

- [ ] Two devices in Settings → Devices: real camera + "Clone Camera"
- [ ] Both show live sensor data (temperature, humidity, light)
- [ ] Both camera streams work simultaneously
- [ ] Switches work independently on both (night light, power)
- [ ] Cloud sensors (motion, sound) work on real camera, unavailable on clone
- [ ] Options flow shows camera selector with both cameras
- [ ] `just dev-restart` → both cameras reconnect
- [ ] Remove the code change, `just dev-restart` → only real camera remains

### Cleanup

Remove the three injected lines from `hub.py`. Verify the file matches the committed version:

```bash
git diff custom_components/nanit/hub.py  # should be empty
```

## C. Migration (v1 → v2)

If testing upgrade from an existing single-camera installation:

1. Start with the old code (v1), add integration, verify camera works
2. Replace `custom_components/nanit/` with the new code
3. `just dev-restart`
4. Check logs for: `Migrated Nanit config entry to version 2`

### Verification

- [ ] Camera still works, same entities, same entity IDs
- [ ] Dashboards and automations unchanged
- [ ] Settings → Nanit → Configure shows per-camera IP options flow
- [ ] Previously configured camera IP preserved in new options
