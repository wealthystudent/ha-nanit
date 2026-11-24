# Nanit — Home Assistant Integration

<p align="center">
  <img src="custom_components/nanit/brand/icon@2x.png" alt="Nanit" width="128" />
</p>

<a href="https://github.com/wealthystudent/ha-nanit">
  <img src="docs/star-banner.svg" alt="Star this repo to help us get an official Nanit API" width="100%" />
</a>

---

> **Monitor your baby — right from Home Assistant.**
>
> Live streams, nursery sensors, night light control, and automations — all from your HA dashboard. Works with all Nanit cameras and the Sound & Light Machine.

<p align="center">
  <img src="docs/images/nanit-card.png" alt="Nanit dashboard card" width="420" />
</p>

## Requirements

- Home Assistant **2025.12** or newer
- A Nanit account with email/password
- [HACS](https://hacs.xyz/) (recommended)

## Installation

### HACS (recommended)

1. Open **HACS → Integrations → ⋮ → Custom repositories**.
2. Add `https://github.com/wealthystudent/ha-nanit` as **Integration**.
3. Install **Nanit**, then restart Home Assistant.

### Manual

1. Download `nanit.zip` from the [latest release](https://github.com/wealthystudent/ha-nanit/releases/latest).
2. Extract it into `config/custom_components/nanit/` in your Home Assistant configuration directory.
3. Restart Home Assistant.

Use the release zip, not a copy of the repository: the source on `main` carries a placeholder version that release builds replace.

## Setup

1. Go to **Settings → Devices & Services → Add Integration → Nanit**.
2. Enter your Nanit email and password.
3. Enter the MFA code sent to your device (use the latest code — they expire quickly).
4. Done — all devices on your account (cameras and Sound & Light Machines) are discovered automatically.

A Sound & Light Machine that isn't paired to any baby in the Nanit app is picked up too, as its own device named after the machine. This is new, so please open an issue if one shows up but doesn't respond.

## What you get

**Per camera:**
- 📷 Live camera stream (RTMPS)
- 🌡️ Temperature & humidity sensors
- 👁️ Motion & sound detection
- 💡 Night light switch
- 🔌 Camera power switch
- 🍼 Care Logs: last diaper change, feeding and pumping, plus today's diaper and feeding counts, bottle and pumped volume, and nursing time, a calendar of every entry, and hourly long-term statistics (see [Care Logs](#care-logs))

**Sound & Light Machine** (works standalone or paired with a camera):
- Power, sound, and light switches
- Sound track selector, volume & brightness controls
- Temperature & humidity sensors
- Battery level & charging status
- Firmware version (diagnostic)
- WiFi signal strength (diagnostic, disabled by default)
- Clock display switch & clock brightness

Some entities are disabled by default. Enable them in **Settings → Devices & Services → Nanit → Entities**.

> Coming from [nanit-sound-light](https://github.com/com6056/nanit-sound-light)? That integration has merged into this one. Its [migration guide](https://github.com/com6056/nanit-sound-light/blob/main/MIGRATION.md) maps every entity id.

## Care Logs

The diaper changes, bottles, nursing and pumping sessions you log in the Nanit app's Care Logs show up as sensors for each baby: on the baby's camera device when that camera is set up, and otherwise on their own, named after the baby (such as "Spare room last diaper change"). A baby without a camera gets them too, once the integration itself has set up, which needs at least one camera or Sound & Light Machine on the account to start. The sensors belong to the baby and to the sign-in: two sign-ins that see the same baby each get their own set, and a camera moved to another baby doesn't carry the first baby's sensors over. Care Logs don't need an Insights plan; Nanit offers them to Owner and Admin accounts. Naps, solids and medications aren't exposed yet.

| Sensor | Shows |
|---|---|
| Last diaper change | When, with the type (pee, poo, mixed or dry) as an attribute |
| Last feeding | The newer of the last bottle and the last nursing session, with the bottle's amount in mL or each side's minutes as attributes |
| Last pumping | When, with the amount in mL and the minutes as attributes |
| Diaper changes today | How many since midnight, with a count for each type as attributes |
| Feedings today | Bottles and nursing sessions since midnight, with a count of each as attributes |
| Bottle volume today | Total logged in bottles since midnight |
| Nursing time today | Minutes of nursing since midnight, both sides together |
| Pumped volume today | Total pumped since midnight |

The volume sensors report millilitres, which Home Assistant shows in fluid ounces when your unit system is US customary. The "today" sensors start again from zero at midnight in Home Assistant's time zone.

The sensors are read-only: entries are added, changed and deleted in the Nanit app. A change to an entry from the last 24 hours, including one logged after the fact, shows up within 5 minutes. A change to an entry from the last 7 days shows up after the integration next reloads. If the account has no Care Logs from the last 7 days when the sensors are first added, they are added disabled. Enable them under **Settings → Devices & Services → Nanit → Entities** once you start logging. If Nanit can't be reached for them when the integration starts, the sensors are added enabled, show unavailable, and fill in at the first poll that succeeds. If Nanit refuses a request for a baby's Care Logs (HTTP 403, for example for an account whose role can't read them), the sensors show unavailable and the integration asks again once an hour instead of every 5 minutes, or at once when it reloads. If Nanit asks it to slow down (HTTP 429), it asks nothing for as long as Nanit asks, at least 5 minutes (15 minutes when Nanit doesn't say); during a wait of more than an hour the sensors check once an hour whether it is over. Either answer holds back every request for that baby's Care Logs on that sign-in, the sensors', the calendar's and the statistics import's alike, whichever of them got it.

A few limits to know about:

- After a restart or reload, the sensors look back only 7 days. A "last" sensor whose entry is older than that shows unknown until you log a newer one.
- A nursing session counts toward the day it started, even when it runs past midnight.
- The "today" sensors start again at the first poll after midnight, so up to 5 minutes late.
- An entry dated up to an hour in the future already counts.
- The "today" sensors don't record long-term statistics of their own. For charts, use the [care log statistics](#care-log-statistics). The Nanit app is the record.

Like any sensor, these are kept in Home Assistant's history, visible to everyone who can use your Home Assistant, and available to voice assistants you expose them to.

Setup and **Settings → Devices & Services → Nanit → Configure → Care log statistics and notes** offer two choices. **Import care log statistics** is on by default for a new setup, and **Show notes and who logged each entry** is off. If you set up the integration before these choices existed, both are off until you turn them on. The notes choice controls the [care log calendar](#care-log-calendar), and the statistics choice the [care log statistics](#care-log-statistics). Changing either reloads the integration.

### Care log calendar

Each baby gets a read-only **Care log** calendar, on the camera's device (a baby without a camera gets one of its own, named after the baby). It shows the diaper changes, bottles, nursing and pumping sessions logged in the app, and any other kind of entry by its name. Sleep entries are not shown yet.

- Each event's title is the kind and its main number, such as "Bottle, 30 mL" or "Nursing, 15 min". Volumes follow your unit system. The description has the rest: each side's minutes or amounts, and when the entry was logged.
- Nursing runs for both sides' minutes together and pumping for its duration. A diaper change, a bottle, or an entry without a length is shown as a one-minute event from the moment it happened, cut short at midnight so it stays on its day, and just before the clocks go back. A range includes it when it happened inside the range. A session that crosses a backward clock change and is shorter than the amount the clocks go back would end before it started on the clock, which Home Assistant rejects, so it is shown the same way, with its real length still in its title (for pumping and other kinds, in its description).
- Entries are fetched from Nanit when the calendar is opened or asked for a range, and kept in memory for up to 5 minutes, then dropped. Nothing is stored. A range of more than 25,000 entries is shown but not kept. Depending on the Home Assistant version, an open calendar view refreshes at each Care Logs poll, or only when you change the view or reload the page; an answer fetched less than 5 minutes earlier can be reused for it. If Nanit can't be reached, the calendar shows an error for that range. After Nanit refuses or throttles a request for the baby's Care Logs, the calendar shows that error without asking Nanit until the wait described above for the sensors is over. After a refusal it also stops showing what it kept or was still fetching. While Nanit throttles, an answer fetched less than 5 minutes earlier is still shown.
- Nanit finds entries by when they started, so the calendar also asks for the 24 hours before the range, to catch a session that began earlier. A session that began more than 24 hours earlier can be missed. Nothing before 2014 or more than a year ahead is asked for, so a range outside those dates is empty.
- The calendar never has a current event: its state is always off, so nothing from an entry reaches Home Assistant's history that way.

With **Show notes and who logged each entry** on, each event's description also shows its note and "Logged by" the person's name and relation to the baby. Anyone who can read the calendar can see them, automations that use the calendar can copy them, and a voice assistant you expose the calendar to can receive them. On current Home Assistant versions a user needs read permission for the calendar; on 2025.12 any signed-in user can read calendar events. Home Assistant's debug logs and backups can hold copies of what an automation did with them. Home Assistant can also write a calendar answer, notes and names included, to its own log without any automation: with debug logging on for `homeassistant.components.websocket_api`, it logs every message it sends to an open calendar view, and when a browser can't keep up it logs an error that includes the last message waiting to be sent. Check the log before you share it. Even when this is off, the calendar shows care routines, times and amounts. Changing the choice reloads the integration and drops everything the calendar kept in memory.

### Care log statistics

With **Import care log statistics** on, each baby's care logs are saved as hourly long-term statistics in Home Assistant's recorder, for charts over weeks and months. Each baby gets up to 16:

- Diaper changes, and one for each type (pee, poo, mixed and dry)
- Bottle feeds and bottle volume
- Nursing sessions and nursing time, together and for each side
- Pumping sessions, pumped volume (together and for each side) and pumping time

Counts are numbers of entries, volumes are in mL and times in minutes. The names begin with the baby's name, such as "Nursery bottle volume". The ids look like `nanit:<code>_bottle_volume`, where the code is a one-way code made from the baby's Nanit id, not the id itself. Pick them in a **Statistics graph** card, or find them in **Developer tools → Statistics** (**Settings → Tools → Statistics** on newer Home Assistant versions). A statistic appears with its first entry that adds an amount above zero, and starts at that entry's hour: a bottle logged as 0 mL counts as a bottle feed, but does not start the bottle volume statistic.

To chart a daily total, add a **Statistics graph** card with the statistic, set the period to **Day**, the chart type to **Bar** and the stat type to **Change**: each bar is that day's total. These statistics are running totals, so **Sum** draws the total since the statistic began instead, which only goes up.

When entries count:

- An entry counts in the hour it is dated. A nursing or pumping session's whole time counts in the hour it started.
- Only finished hours are written, at the first read asked for after the hour ends, so a few minutes after the hour.
- An hour is written only once a read has covered it and every hour before it, back to the start of the history. A read looks back a day, or 7 days after a break of more than a day. If that doesn't reach the last hour already read (after a break of just under a day, or of more than 7 days), nothing later is written until the whole history is read again, which then happens within the hour. If Nanit can't be reached for that read, it is tried again, at first after 5 and then 15 minutes, then every hour, and nothing later is written until it gets through.
- The rows are on UTC hours. In a time zone offset from UTC by 30 or 45 minutes, a day starts in the middle of a row, so a daily bar can include a slice of the neighbouring day, and a daily chart can differ slightly from the matching "today" sensor.

Corrections:

- A new entry, or a raised amount, is written as soon as the integration reads it: within about 5 minutes for an entry dated in the last 24 hours, and at the next read of the whole history for an older one.
- Anything that would lower a number (a deleted entry, a smaller amount, an entry moved to another time, a changed diaper type) is written only after a second read from Nanit, at least 5 minutes later, agrees. An answer from Nanit that holds an entry of any kind dated outside the time range that was asked for is never used to confirm such a change, and that entry is left out of the statistics and the sensors.
- Nanit doesn't say when an entry was deleted, only that it is no longer there. So there is a small chance that a deleted entry lingers, or that an entry Nanit leaves out of two reads in a row is dropped until Nanit returns it again.
- The whole history is read again once a day and after every restart or reload, with one request per 90 days of history. It starts a year before the baby's birth date or the date the baby was added to Nanit, whichever is earlier (three years back when Nanit gives neither). If the reads of the whole history after a restart keep disagreeing (three in all), the import leaves the statistics as they are and tries again a day later. Rows before the first whole UTC hour the history read covers are left as they are, so two cases can't be exact. An entry already counted in those older rows and then re-dated to after that hour is counted twice; that can only happen when the start moves later: when Nanit gives neither date and the three-year start moves on, or when the birth date is changed to a later one. And an entry already counted after that hour and then re-dated to before it is taken out of the newer rows but not added to the older ones.
- If Nanit answers one of those 90-day requests with more than the integration reads (16 MiB), the diagnostics show `history_too_large` and the log has one warning: that read of the whole history stops there and is tried again only a day later, and after a restart nothing is imported for the baby until one gets through.
- A restart after which the import would lower a number already stored needs two whole-history reads that agree, and a read that holds a row Nanit sent in a form the integration can't use (or dated outside the range asked for) never counts as agreeing. So if one stretch of the history always comes back with such a row, the reads never agree: nothing new is written for that baby, not even today's entries, the import gives up for a day after the third read and tries again (the diagnostics show `waiting_for_second_pass` between the reads, then `gave_up`, with `give_ups` counting each time), until Nanit stops sending that row. The care log sensors and the calendar aren't affected.
- An amount or time Nanit sends but that can't be used leaves that hour's volume or time as it was. The entry still counts. When one side of a pumping or nursing entry can't be used, any change to that entry that would lower a number also waits until Nanit sends a usable value again.
- Every write is read back. If the recorder took a write but it can't be confirmed (the recorder is very busy or failing), the write may still be stored later, so the import for that baby pauses until Home Assistant restarts: nothing more is read from Nanit for it or written (the 5-minute care log reads made for the statistics stop too, while enabled care log sensors and the care log calendar still read on their own), the log has one warning, and the integration's diagnostics show `paused_until_restart` for the baby. After the restart the import compares what the recorder really holds with what it reads from Nanit and corrects it, a lowered number again only after a second read. A write the recorder refuses, or a recorder that isn't ready, only delays the import.

What it costs, measured with made-up history:

- At most about 16 rows per hour per baby, one for each statistic from its first entry on, so fewer when a kind is never logged.
- One baby with a year of history is roughly 140,000 rows, about 10 MB of database, and a minute or two for the first import, which runs in the background.
- An extreme case, three babies with five years each, was about 2.1 million rows, 146 MB and 23 minutes.

Privacy and control:

- The statistics show care routines, times and amounts to anyone who can read statistics in this Home Assistant. The recorder's include and exclude settings don't apply to them, and they stay until you delete them.
- Turning **Import care log statistics** off stops the import and leaves what was already imported.
- To delete them for good, in this order: turn the import off on every Nanit sign-in in this Home Assistant that sees the baby, restart Home Assistant, then in **Developer tools → Statistics** search for the baby's name, select the statistics and choose **Delete selected statistics**. The restart makes sure no write of the import is still on its way to the recorder; one that is can bring rows back after you delete them, and the integration can't always show that one is. A sign-in that still has the import on writes them again: the one that started them, or another one that starts them afresh once they are gone (see below).
- On disk, the integration keeps one small file for this, `.storage/nanit.care_log_statistics` in the configuration folder: two one-way codes per baby, one made from the baby's Nanit id and one from the email address of the Nanit sign-in that writes its statistics. It holds no entries and no times, and it stays when the integration is removed (see [Removing the integration](#removing-the-integration)). If Home Assistant can't read it, for example a file a newer version of the integration wrote in a format this one doesn't support, no sign-in writes, the log has one error, the integration's diagnostics show `owner_unknown`, and it tries again every 15 minutes. A file a newer version wrote in a compatible format (a newer minor version) is read, and saved again in this version's format. A file that is no longer valid JSON is moved aside by Home Assistant (as `nanit.care_log_statistics.corrupt.` and the time, with a repair issue) and read as empty: then no sign-in is on record as the owner, so while the baby's statistics exist none writes them (`unowned`, see below), and once there are none a sign-in starts them afresh. What the import has read from Nanit is held in memory, and read again after a restart.
- When two Nanit sign-ins in this Home Assistant see the same baby and both have the import on, only the sign-in that started that baby's statistics writes them. A sign-in starts them only while the recorder holds none of the baby's statistics, never while it has polling turned off in its system options, and only once it has read the baby's care logs from Nanit: a sign-in Nanit refuses them to doesn't start them, so another one that can read them does, and a refused one that later gets through starts them then, if no other sign-in has. The other never writes, even while the first is removed, has the import turned off, has polling turned off or can't sign in. Removing and adding the same Nanit account again keeps it as the writer, whatever the letter case of its email address. To switch to the other sign-in, delete the baby's statistics for good as above (the import off on every sign-in that sees the baby, a restart, then the deletion), and only then turn the import on for the sign-in that should write them: it imports them afresh. That is a rebuild from what that sign-in can read from Nanit now, so anything only the first sign-in could see is gone from the statistics.
- If the recorder holds statistics for a baby but this Home Assistant has no record of which sign-in started them (for example after restoring the database without the `.storage` folder), no sign-in writes them: the integration's diagnostics show `unowned` for the baby, and the log has a warning once per start of Home Assistant. To recover, decide which sign-in should own them, delete the baby's statistics for good as above (the import off on every sign-in that sees the baby, a restart, then the deletion), then turn the import on for that sign-in, and only after that for any other. That sign-in then imports them afresh from what it can read from Nanit.
- With debug logging turned on for Home Assistant's recorder, and in some of its error messages, the log shows each write of the import in full: the baby's display name in the statistic names, the statistic ids, and every hour's time and numbers (never notes or caregiver names). Remove those lines, or the names and numbers in them, before you share such a log.
- The import needs Home Assistant's recorder. Without it nothing is imported, and Nanit is asked for nothing beyond the usual care log reads. While recording is paused with the `recorder.disable` action, the import makes no new requests of its own and writes nothing (a request already on its way still completes, and what it read is kept in memory), and it carries on after `recorder.enable`.

The five "today" sensors no longer record long-term statistics of their own; these statistics take their place, whether or not the import is on.

## Read-only mode (optional)

Turn on **Read-only mode** during setup, or later under **Settings → Devices & Services → Nanit → Configure → Read-only mode**, if Home Assistant should only *watch*. It gives Home Assistant no control over device settings:

- You get the camera stream, temperature and humidity, motion and sound detection, the Care Logs sensors and the care log calendar. (Viewing the stream still asks the camera to stream, as it always does.)
- There are no switches, lights, numbers, selects or media players. Nothing in Home Assistant (dashboards, automations, scripts or a voice assistant) can put the camera to sleep, change the night light or volume, or control a Sound & Light Machine.
- The camera has no on/off. A direct `camera.turn_off` or `camera.turn_on` call is refused.
- A read-only **Camera Power** binary sensor (on = awake, off = asleep) takes the place of the power switch, so you can still see, and automate on, whether the camera is asleep. It exists only in this mode.

Turning it on removes the control entities, so **automations and scripts that use them stop working** while it's on. Turning it off brings them back with the same names and entity IDs, so those automations work again. **Turning it off also removes the Camera Power sensor, so automations that use it stop working**; switch them to the power switch. The dashboard card stays available and still shows "Camera Off" while the camera is asleep, but its controls do nothing in this mode. The setting applies to every device on the account.

## Stills from the live stream (optional)

Turn on **Settings → Devices & Services → Nanit → Configure → Stills from the live stream** to fill the camera's picture from its live stream. Nanit's cloud currently returns no still picture, so without it the camera's picture in Home Assistant, and anything built on it such as a HomeKit camera tile or a picture card, stays empty ("No Response" in HomeKit) until someone opens the live view.

How it works:

- When something asks for a picture, the cloud has none, and the last picture is more than about 10 minutes old, Home Assistant starts the camera's stream briefly and keeps a single frame. Until that frame arrives, the previous picture (or none) is shown.
- Nothing is taken while the camera is asleep or unreachable. If stills keep failing, Home Assistant retries less often, up to once an hour, and logs one warning.
- It is off by default, and it changes nothing while the cloud picture works. It works in read-only mode, since it only watches.

What it costs:

- The camera keeps streaming for about 20 minutes after each still. Something that keeps asking for a picture, like HomeKit or a dashboard that is always on screen, therefore keeps the camera streaming all the time.
- While it streams, it holds one of the camera's stream slots (it allows about two), so one fewer phone can watch live in the Nanit app.
- It needs FFmpeg. Home Assistant OS and Container include it. On a Core install without FFmpeg, nothing is sent to the camera and one warning is logged. Install FFmpeg, then restart Home Assistant. Without it, Home Assistant's FFmpeg integration, which Nanit depends on, also logs an error at startup, whether or not this option is on.

The setting applies to every camera on the account, and changing it reloads the integration.

## Dashboard Card

A companion Lovelace card is **bundled with the integration** — no HACS frontend dependencies or manual JS installation required. After setup, the card appears in your card picker automatically.

**To add it:** Open any dashboard → **Add Card** → search for **Nanit** → select your camera.

The card provides:
- Live camera stream with loading indicator
- Temperature & humidity overlays, with optional semantic entity overrides for the displayed sensors
- Motion & sound activity indicators
- Optional baby name and connectivity status display
- Header power button (can be hidden in card settings)
- Night light slider (drag to adjust brightness, 0% = off; can be hidden in card settings)
- Sound machine controls with icon-based track selection (can be hidden in card settings)
- Volume slider
- Network info popup (WiFi name, frequency, signal strength)

Optional sensor overrides can be set in the visual editor or YAML when you want the overlay to display another Home Assistant sensor instead of the Nanit-discovered sensor:

```yaml
type: custom:nanit-card
camera_entity_id: camera.nursery
temperature_entity_id: sensor.nursery_temperature
humidity_entity_id: sensor.nursery_humidity
```

> [!NOTE]
> If your Lovelace is in **YAML mode**, add the resource manually:
> ```yaml
> resources:
>   - url: /nanit-card/nanit-card.js
>     type: module
> ```

## Actions

The integration provides one action:

**`nanit.reset_stream`** targets a Nanit camera entity and discards Home Assistant's cached camera stream, so the next viewer gets a freshly negotiated stream from the Nanit cloud. The bundled dashboard card calls it automatically when it detects a stalled stream, and it is useful in automations or scripts when a stream shows a stale or frozen picture. It has no parameters beyond the target.

```yaml
action: nanit.reset_stream
target:
  entity_id: camera.nursery
```

## Data updates

How each piece of data reaches Home Assistant:

- **Camera sensors** (temperature, humidity, night light, connectivity) arrive as push updates over the camera's WebSocket, so they update in real time.
- **Motion and sound events** come from the Nanit cloud API, polled every 30 seconds.
- **Network diagnostics** (WiFi details) are polled every 5 minutes.
- **Care Logs** are polled every 5 minutes. The first poll after setup looks back 7 days for the last entry of each type; later polls fetch the last 24 hours. A poll looks back 7 days again after more than 24 hours without a successful one. The care log calendar fetches the range it is asked for, in pieces of up to 90 days, and keeps the answer for up to 5 minutes.
- **Sound & Light state** (power, sound, light, volume) arrives as push updates over the speaker's WebSocket, local or relay. A light 30 second poll reconciles state and refreshes battery and WiFi diagnostics. The firmware version is requested with the poll until known, then left alone.
- **Video** streams on demand when a viewer opens the camera: straight from the camera over your network with [local streaming](#local-streaming), or through the Nanit cloud over RTMPS.

## Local connection

The Sound & Light Machine is controlled over your network when it can be. It is discovered on the LAN automatically (mDNS), and the local connection is preferred whenever the speaker is reachable, with the cloud relay as fallback. If discovery doesn't find a machine that's paired with a baby, set its IP under **Settings → Devices & Services → Nanit → Configure → Sound & Light IP address**, which takes precedence over discovery.

The camera is always controlled through Nanit's relay. Current camera firmware doesn't accept the integration on its local port, so there is no camera IP option.

One thing worth knowing: the speaker accepts a single local client at a time. If the Nanit phone app on the same network holds the local slot, the integration uses the cloud relay and takes the local slot back automatically when it frees up.

## Local streaming

Local streaming is on by default, unless Home Assistant's go2rtc uses an external server (see below). When someone opens the camera, Home Assistant asks the camera to send its video and audio straight to Home Assistant over your network, instead of through the Nanit cloud. Control still goes through Nanit's relay as before. The video is encrypted (RTMPS, with a certificate Home Assistant creates for itself), which keeps it from being read on your network but doesn't authenticate either end. Home Assistant re-serves it to its own players, so the live view, WebRTC and HLS all work the same.

The camera has to be able to reach Home Assistant on TCP port 1935. Home Assistant only accepts connections on it from local addresses (private, link-local or loopback), so even a host with a public IPv6 address doesn't take them from the internet.

On Home Assistant OS there is nothing to do. On Container or Core with host networking, allow TCP 1935 if the host runs a firewall (firewalld, ufw), for example `sudo firewall-cmd --permanent --add-port=1935/tcp && sudo firewall-cmd --reload`. With Docker bridge networking, publish the port (`-p 1935:1935`) and set **Advertised address** to the host's LAN IP in the options. If the camera is on another network or VLAN, allow it to reach Home Assistant on TCP 1935 in your router. If Home Assistant has its own address on the camera's network, set **Advertised address** to that address instead.

If the camera can't reach Home Assistant, nothing breaks: the integration falls back to the cloud stream. After two misses in a row it pauses local streaming, retries it automatically once a day, and shows a repair in **Settings → System → Repairs** with the exact address the camera was given and a button to retry once you've fixed it.

**Settings → Devices & Services → Nanit → Configure → Local streaming** turns it off, changes the port, or sets the advertised address. If the port is taken, the next few ports are tried automatically unless an advertised address is set. With more than one Nanit account, the first one to start sets the port for all of them.

Good to know:

- The camera only streams to a few Nanit apps or devices at a time (two in testing), and Home Assistant counts as one. When other apps already use them all, Home Assistant falls back to the cloud stream and tries local again later, waiting longer each time the camera is still busy, up to six hours.
- In testing, Nanit's cloud stream kept playing even while the camera refused Home Assistant's own stream for being busy. When Home Assistant falls back to it, the Nanit card reloads its player right away, and keeps its loader up meanwhile instead of showing the player's error, so the first view isn't left on an error. The camera entity's `stream_route` attribute says which one a new view gets (`local` or `cloud`).
- The camera keeps sending video to Home Assistant until it's told to stop, so Home Assistant stops it about two minutes after the last viewer leaves. Opening the camera again starts it again within a few seconds.
- If your go2rtc server's URL (`go2rtc: url:` in your configuration) isn't `localhost` or a loopback address, it can't reach the local stream, so WebRTC would fall back to HLS. Local streaming therefore starts off on those setups. You can still turn it on in the options.
- A camera that falls back to the cloud goes back to local when the wait is over, even while it's being watched, preloaded, or asked for stills. Home Assistant first tells the camera to stop its own cloud stream, which would otherwise hold one of the camera's slots. A WebRTC viewer's picture pauses until the player reconnects, which the Nanit card does on its own.
- Reloading the integration ends that wait, so local is tried again on the next view. It also ends the pause after two misses in a row, as the repair's retry button does. Restarting Home Assistant keeps the pause.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| MFA code rejected | Codes expire fast — use the latest one. |
| Stream not playing | Verify HA can reach `rtmps://media-secured.nanit.com`. The Stream integration is pulled in automatically; on installs without `default_config:` an older version left it out, and the picture silently fell back to still images. |
| Local streaming not used | Check **Settings → System → Repairs** for a Nanit repair, which names the address the camera was told to use. Allow TCP 1935 to Home Assistant in the host firewall or your router. In a container or with a second network, you may also need the advertised address. See [Local streaming](#local-streaming). |
| Local streaming keeps getting declined with nothing else streaming | The camera can get stuck refusing new streams even when nothing else is watching. To confirm, turn on **Enable debug logging** from the integration's menu. A stuck camera answers `403 Forbidden: Number of Mobile App connections above limit` with no other Nanit app or device in use. Unplug it for about 30 seconds and plug it back in. |
| FFmpeg error at startup (Home Assistant Core) | Nanit depends on Home Assistant's FFmpeg integration (for stills from the live stream), which logs an error at startup when no ffmpeg binary is installed, whether or not that option is on. Install FFmpeg and restart Home Assistant. Home Assistant OS and Container include it. |
| Stream frozen or stale | Run the `nanit.reset_stream` action on the camera entity (the dashboard card does this automatically when it detects a stall). |
| Sensors unavailable | WebSocket reconnects automatically. Try reloading the integration if it persists. |
| Temperature or humidity shows unknown (`--` on the card) | The camera isn't reporting a reading. The Nanit app shows dashes for it too. |
| Sound & Light not using the local connection | Check that it's on the same network as HA, or set its IP in the options. The Nanit app on the same network can hold the speaker's only local slot. |
| Re-authentication required | Session expired — click the notification to re-enter credentials. |
| Other issues | Check **Settings → System → Logs** (filter for `nanit`) or download diagnostics from the integration page. |

## Known limitations

- Authentication, motion/sound events and camera control always require the Nanit cloud, so there is no fully offline mode. Video stays on your network with [local streaming](#local-streaming).
- Motion and sound detection is polled every 30 seconds (up to ~30s delay).
- Without local streaming, live video requires your HA instance to reach `rtmps://media-secured.nanit.com`.

## Removing the integration

1. Go to **Settings → Devices & Services → Nanit**, open the three dot menu on the entry, and choose **Delete**. This removes all Nanit devices and entities and deletes the stored account tokens.
2. If you installed through HACS, also remove the repository there: **HACS → Nanit → three dot menu → Remove**, then restart Home Assistant.

Home Assistant does not remove two things with the integration:

- The care log statistics, if the import was on. They stay in the recorder until you delete them in **Developer tools → Statistics** (see [Care log statistics](#care-log-statistics)). Restart Home Assistant after removing the integration, and only then delete them: the restart makes sure no write of the import is still on its way to the recorder.
- The file `.storage/nanit.care_log_statistics` in the configuration folder, once a baby's statistics were started: two one-way codes per baby, one made from the baby's Nanit id and one from the email address of the sign-in that writes its statistics, and nothing else. You can delete it once no Nanit entry is left. If you delete it while the statistics still exist and add Nanit again, no sign-in writes them until you follow the steps for `unowned` in [Care log statistics](#care-log-statistics).

The integration keeps no other state on disk. If you use the bundled dashboard card in YAML mode, remove its resource entry as well.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and PR workflow.

## License

[MIT](LICENSE)
