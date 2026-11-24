# Testing: Care Logs

Step-by-step guide to check the care log sensors, calendar and long-term statistics against the Nanit app, with the dev HA instance.

**Related:** PR #220, Issue #49

You need a Nanit account that can read Care Logs (an Owner or Admin account; no Insights plan is needed) and some entries from the last few days. Part 6 logs one test entry in the Nanit app and deletes it again; nothing else changes your data.

---

## Part 1 — Run the unit tests

These test the care log code without an account.

```bash
just test all
```

Expected: everything passes. The care log tests are `tests/unit/test_care_log*.py`, `tests/unit/recorder/test_care_log_*.py` and the calendar tests in `packages/aionanit/tests/test_rest.py`.

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
- [ ] **Settings → Devices & Services → Nanit → Configure → Care log statistics and notes** shows both choices ticked

---

## Part 3 — Sensors

Open the baby's camera device (a baby without a camera has a device of its own, named after the baby).

- [ ] Eight care log sensors: Last diaper change, Last feeding, Last pumping, Diaper changes today, Feedings today, Bottle volume today, Nursing time today, Pumped volume today
- [ ] Each "last" sensor shows the newest entry of its kind in the Nanit app, with its type, amount or minutes as attributes
- [ ] The "today" sensors match today's entries in the app, counted from local midnight

The app's own list rounds some amounts and drops seconds from nursing times, so a difference of a few mL or a minute is expected. If the account has no Care Logs from the last 7 days, the sensors are added disabled: enable them under **Entities**.

---

## Part 4 — Calendar

Open **Calendar** and tick the baby's **Care log** calendar.

- [ ] Today's entries match the app by kind, time and amount, such as "Bottle, 90 mL" or "Nursing, 15 min"
- [ ] Opening a bottle shows when it was logged, its note and "Logged by" the person's name and relation
- [ ] With **Show notes and who logged each entry** unticked in the options, the note and "Logged by" are gone; tick it again afterwards

---

## Part 5 — Long-term statistics

The first import reads the whole history in the background; a year of history takes a minute or two.

- [ ] **Settings → Tools → Statistics** (**Developer tools → Statistics** on older versions) lists the baby's statistics, such as "<baby name> Bottle volume"
- [ ] A **Statistics graph** card with that statistic, chart type **Bar**, period **Day** and stat type **Change** shows one bar per day; a few past days match the app's totals

---

## Part 6 — A new entry and a deleted one

1. In the Nanit app, log a diaper change dated two hours ago (an hour that has finished, since only finished hours are written)

- [ ] Within about 5 minutes: **Diaper changes today** goes up by one, the calendar shows the entry, and today's bar for "<baby name> Diaper changes" goes up by one

2. Delete that entry in the app

- [ ] Within about 5 minutes: the sensor and the calendar drop it
- [ ] Within about 15 minutes: the bar drops it too (a lowered number waits for a second read, at least 5 minutes later, that agrees)

---

## Part 7 — Restart

```bash
just dev restart
```

- [ ] The sensors come back with the same values
- [ ] The charts are unchanged

---

## Part 8 — Import off and on

1. In **Configure → Care log statistics and notes**, untick **Import care log statistics** and submit

- [ ] The statistics are still listed and charted, and stop updating

2. Tick it again and submit

- [ ] Within a few minutes the charts catch up, including entries logged while the import was off

---

## Part 9 — Stop the dev instance

```bash
just dev stop
```

To wipe all state, including the imported statistics, for a fresh test next time:

```bash
just dev reset
```
