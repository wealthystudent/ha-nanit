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

Temporarily inject a fake second baby so the hub discovers two cameras. Your real camera works normally; the fake one appears as a separate device (unavailable, since there's no physical camera behind it).

**Step 1:** Edit `custom_components/nanit/hub.py`, in `async_setup()`, add after the `babies = await self._client.async_get_babies()` line:

```python
# --- DEV ONLY: simulate a second camera. Remove before committing. ---
from aionanit.models import Baby
babies.append(Baby(uid="fake_baby_2", name="Test Baby 2", camera_uid="fake_cam_2"))
```

**Step 2:** Restart HA (or `docker compose restart`).

**Step 3:** Verify:

- [ ] Two devices appear in Settings → Devices: your real camera + "Test Baby 2"
- [ ] Real camera entities work normally (temp, humidity, stream, switches)
- [ ] "Test Baby 2" entities show as unavailable (expected — no physical camera)
- [ ] Each device has independent entities with unique IDs
- [ ] Options flow: Settings → Nanit → Configure → shows camera selector with both cameras
- [ ] Set a local IP for the fake camera → entry reloads, real camera still works
- [ ] Remove the dev hack, restart → only your real camera remains

**Step 4:** Remove the hack from `hub.py` before committing.

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
