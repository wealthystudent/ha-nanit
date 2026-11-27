# Testing: Care Logs

Step-by-step guide to check the care log sensors and calendar against the Nanit app, with the dev HA instance.

**Related:** PR #220, Issue #49

You need a Nanit account that can read Care Logs (an Owner or Admin account; no Insights plan is needed) and some entries from the last few days. Part 5 logs one test entry in the Nanit app and deletes it again; nothing else changes your data.

---

## Part 1 — Run the unit tests

These test the care log code without an account.

```bash
just test all
```

Expected: everything passes. The care log tests are `tests/unit/test_care_log*.py`, `tests/unit/test_care_logs.py` and the calendar tests in `packages/aionanit/tests/test_rest.py`.

---

## Part 2 — Start the dev HA instance and add Nanit

```bash
just dev
```

Open http://localhost:8123, then:

1. Go to **Settings → Devices & Services → Add Integration** and search for **Nanit**
2. Tick **Read-only mode**: this guide only watches
3. Enter your Nanit **email** and **password**, then the code Nanit sends you

**Verify:**
- [ ] **Settings → Devices & Services → Nanit → Configure → Care log notes** shows **Show notes and who logged each entry** unticked

---

## Part 3 — Sensors

Open the baby's camera device. A baby without a camera has no device: find its sensors under **Settings → Devices & Services → Entities** by the baby's name.

- [ ] Eight care log sensors: Last diaper change, Last feeding, Last pumping, Diaper changes today, Feedings today, Bottle volume today, Nursing time today, Pumped volume today
- [ ] Each "last" sensor shows the newest entry of its kind in the Nanit app, with its type, amount or minutes as attributes
- [ ] The "today" sensors match today's entries in the app, counted from local midnight

The app's own list rounds some amounts and drops seconds from nursing times, so a difference of a few mL or a minute is expected. If the account has no Care Logs from the last 7 days, the sensors are added disabled: enable them under **Entities**.

---

## Part 4 — Calendar

Open **Calendar** and tick the baby's **Care log** calendar.

- [ ] Today's entries match the app by kind, time and amount, such as "Bottle, 90 mL" or "Nursing, 15 min"
- [ ] Opening a bottle shows when it was logged, but no note and no "Logged by"
- [ ] With **Show notes and who logged each entry** ticked in the options, the bottle also shows its note and "Logged by" the person's name and relation

---

## Part 5 — A new entry and a deleted one

1. In the Nanit app, log a diaper change dated earlier today

- [ ] Within about 5 minutes: **Diaper changes today** goes up by one and the calendar shows the entry (reopen the calendar or reload the page: an open view can keep its answer for up to 5 minutes)

2. Delete that entry in the app

- [ ] Within about 5 minutes: the sensor and the calendar drop it

---

## Part 6 — Restart

```bash
just dev restart
```

- [ ] The sensors come back with the same values

---

## Part 7 — Stop the dev instance

```bash
just dev stop
```

To wipe all state for a fresh test next time:

```bash
just dev reset
```
