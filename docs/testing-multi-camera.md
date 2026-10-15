# Testing: Multi-camera support

Step-by-step guide to verify that multiple cameras on one Nanit account work correctly.

**Related:** PR #12, Issue #9

---

## Part 1 — Run the unit tests

These test multi-camera logic without any hardware.

```bash
just test
```

Expected: **30/30 pass**. If any fail, stop here and fix them first.

---

## Part 2 — Start the dev HA instance

```bash
just dev
```

Wait ~30 seconds for HA to boot, then open http://localhost:8123 in your browser.

If this is your first time, you'll go through the HA onboarding wizard (create user, pick location, etc.). This only happens once.

---

## Part 3 — Add the Nanit integration

1. Go to **Settings → Devices & Services**
2. Click **Add Integration** (bottom right)
3. Search for **Nanit**
4. Enter your Nanit **email** and **password**
5. Click **Submit**
6. If prompted, enter the **MFA code** sent to your phone
7. The integration is now added. Your camera should appear as a device.

**Verify:**
- [ ] Go to **Settings → Devices & Services → Nanit** — you see one device
- [ ] Click the device — you see entities: temperature, humidity, light, night light, camera power, volume, camera, connectivity, motion, sound

---

## Part 4 — Simulate a second camera

Since you only have one physical camera, you'll inject a fake second baby into the code. This makes the integration think there are two cameras on your account.

### 4.1 — Edit hub.py

Open `custom_components/nanit/hub.py` in your editor.

Find this line (around line 96):

```python
        babies = await self._client.async_get_babies()
```

Add these three lines **directly below it**:

```python
        # --- DEV ONLY: remove before committing ---
        from aionanit.models import Baby
        babies.append(Baby(uid="clone_baby", name="Clone Camera", camera_uid="clone_cam"))
```

Save the file.

### 4.2 — Restart HA

```bash
just dev restart
```

Wait ~15 seconds for HA to restart.

### 4.3 — Verify two devices appear

1. Open http://localhost:8123
2. Go to **Settings → Devices & Services → Nanit**
3. You should now see **2 devices**
4. Click each device to confirm:
   - Your real camera — all entities working (temperature has a value, etc.)
   - "Clone Camera" — entities exist but show **unavailable** (expected, no real camera behind it)

- [ ] Two devices visible
- [ ] Real camera entities have live data
- [ ] Clone camera entities show unavailable

---

## Part 5 — Clone stays unavailable

This part used to point the clone at your real camera through the camera IP option. That option is gone, since current camera firmware rejects the integration on its local port (#176), so the clone keeps showing unavailable. Skip to Part 6 and run its checks against your real camera.

---

## Part 6 — Test isolation

Verify that the clone doesn't affect the real camera.

### 6.1 — Restart HA

```bash
just dev restart
```

- [ ] Real camera reconnects after restart and has live sensor data
- [ ] Clone camera still shows unavailable

### 6.2 — Check the options flow skips cameras

1. Go to **Settings → Devices & Services → Nanit → Configure → Sound & Light IP address**
2. Without a Sound & Light Machine paired to a baby, the flow stops with "This account has no Sound & Light Machine paired with a baby." With one, the selector is skipped and that baby's IP form opens straight away. With more than one, only babies that have a machine are listed.

- [ ] Clone Camera never appears in the options flow

---

## Part 7 — Clean up

### 7.1 — Remove the dev hack

Open `custom_components/nanit/hub.py` and delete the three lines you added:

```python
        # --- DEV ONLY: remove before committing ---
        from aionanit.models import Baby
        babies.append(Baby(uid="clone_baby", name="Clone Camera", camera_uid="clone_cam"))
```

Save the file.

### 7.2 — Restart and verify

```bash
just dev restart
```

1. Go to **Settings → Devices & Services → Nanit**
2. Only your real camera should remain

- [ ] Clone Camera is gone
- [ ] Real camera still works normally

### 7.3 — Confirm no leftover changes

```bash
git diff custom_components/nanit/hub.py
```

This should show **no output** (file matches the committed version).

---

## Part 8 — Test remove and re-add

1. Go to **Settings → Devices & Services → Nanit**
2. Click the **three dots** menu → **Delete**
3. Confirm deletion
4. Verify the Nanit integration is gone from the list
5. Add it again (repeat Part 3)
6. Camera reappears with all entities

- [ ] Clean removal — no orphaned entities
- [ ] Clean re-add — everything works fresh

---

## Part 9 — Stop the dev instance

When you're done testing:

```bash
just dev stop
```

To wipe all state for a completely fresh test next time:

```bash
just dev reset
```
