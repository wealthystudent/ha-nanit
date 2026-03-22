# Testing

## 1. Unit tests (no hardware needed)

```bash
# Install (once)
pip install -r requirements-test.txt

# Integration tests (30 tests — config flow, migration, hub, lifecycle)
python -m pytest tests/ -v

# aionanit library tests (183 tests — protocol, REST, auth, transport)
cd packages/aionanit && python -m pytest tests/ -v && cd ../..
```

> **Note:** Run the two test suites separately (not together) due to a `tests/` namespace collision.

### What the integration tests cover

| File | # | Covers |
|------|---|--------|
| `test_config_flow.py` | 12 | Login, MFA, duplicate email abort, options flow (per-camera IP) |
| `test_init.py` | 9 | Setup/unload, auth/connection errors, v1→v2 migration (5 cases) |
| `test_hub.py` | 9 | Multi-baby discovery (1/3/0 babies), partial failure, token refresh |

Multi-camera scenarios are fully tested via mocks — no camera hardware required.

## 2. Dev HA instance (Docker)

```bash
docker compose -f docker-compose.dev.yml up -d     # Start → http://localhost:8123
docker compose -f docker-compose.dev.yml logs -f    # Tail logs (debug logging enabled)
docker compose -f docker-compose.dev.yml restart     # Restart after code changes
docker compose -f docker-compose.dev.yml down        # Stop and remove
```

Source files are mounted read-only — edit code normally, then `docker compose restart`.

State lives in `dev-config/` (gitignored except `configuration.yaml`). To start fresh, stop the container and `rm -rf dev-config/.storage dev-config/home-assistant_v2.db*`.

## 3. Testing with multiple cameras

### If you have multiple cameras

No special setup. Add the integration, enter credentials + MFA, and all cameras appear automatically as separate devices. Verify:

- [ ] All cameras appear as devices in Settings → Devices
- [ ] Each camera has its own entities (sensors, switches, camera stream)
- [ ] Configure per-camera IPs: Settings → Nanit → Configure → select camera → enter IP
- [ ] Restart HA → all cameras reconnect
- [ ] Remove integration → all cameras + entities removed cleanly

### If you have one camera (simulated multi-camera)

Clone your real baby with a different UID so the hub discovers two cameras that both connect to the same physical device over LAN. Both devices will have real live sensor data, switches, and streams.

**Step 1:** Edit `custom_components/nanit/hub.py`, in `async_setup()`, add after the `babies = await self._client.async_get_babies()` line:

```python
# --- DEV ONLY: clone camera for multi-device testing. Remove before committing. ---
from aionanit.models import Baby
real = babies[0]
babies.append(Baby(uid="clone_baby", name="Clone Camera", camera_uid="clone_cam"))
```

**Step 2:** Set the clone's local IP to your real camera's IP. In the options flow (Settings → Nanit → Configure), select "Clone Camera" and enter your camera's LAN IP (e.g. `192.168.1.x`). This makes the clone connect to the same physical camera over the local network.

> The clone's cloud coordinator will fail (the cloud doesn't know `clone_cam`) — this is expected. Cloud-based motion/sound sensors will be unavailable on the clone. All local data (temperature, humidity, night light, camera stream, etc.) will work on both devices.

**Step 3:** Restart HA (or `docker compose restart`).

**Step 4:** Verify:

- [ ] Two devices appear in Settings → Devices: your real camera + "Clone Camera"
- [ ] Both show live sensor data (temperature, humidity, light)
- [ ] Both camera streams work simultaneously
- [ ] Switches work independently on both (night light, power)
- [ ] Cloud sensors (motion, sound) work on the real camera, unavailable on clone (expected)
- [ ] Options flow shows camera selector with both cameras
- [ ] Restart HA → both cameras reconnect
- [ ] Remove the dev hack, restart → only your real camera remains

**Step 5:** Remove the hack from `hub.py` before committing.

## 4. Testing migration (existing v1 → v2)

If you already have the integration installed (v1, single camera):

1. Update the integration code (replace `custom_components/nanit/`)
2. Restart HA
3. Check the HA log for: `Migrated Nanit config entry to version 2`
4. Verify:
   - [ ] Camera still works, same entities, same entity IDs
   - [ ] Dashboards and automations unchanged
   - [ ] Settings → Nanit → Configure now shows the per-camera IP options flow
   - [ ] If you had a local camera IP configured, it's preserved in the new options

## 5. Checklist before release

- [ ] `python -m pytest tests/ -v` → 30/30 pass
- [ ] `cd packages/aionanit && python -m pytest tests/ -v` → 183/183 pass
- [ ] Docker dev instance: add integration, all cameras appear
- [ ] Docker dev instance: options flow works (set/clear camera IP)
- [ ] Docker dev instance: restart HA → cameras reconnect
- [ ] Docker dev instance: remove + re-add integration → clean cycle
- [ ] If testing migration: existing v1 entry migrates cleanly
