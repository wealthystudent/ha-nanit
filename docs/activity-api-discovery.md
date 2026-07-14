# API Discovery Guide: Nanit Care Logs Endpoints

> **Purpose**: Step-by-step guide for a tester with the Nanit **Insights plan** to capture the API endpoints used by the Care Logs feature (feeding, diaper changes, activity log).
>
> **Context**: [Issue #49](https://github.com/wealthystudent/ha-nanit/issues/49) — no public documentation exists for these endpoints. We need someone with an active Insights subscription to intercept the app's network traffic.

---

## Prerequisites

- A phone (iOS or Android) with the **Nanit app** installed and logged in
- An active **Nanit Insights plan** (Care Logs feature must be visible in the app)
- A computer on the **same Wi-Fi network** as the phone
- ~15 minutes

---

## Option A: mitmproxy (Free, Cross-Platform)

### 1. Install mitmproxy on your computer

**macOS:**
```bash
brew install mitmproxy
```

**Linux:**
```bash
pip install mitmproxy
# or: sudo apt install mitmproxy
```

**Windows:**
Download from https://mitmproxy.org/

### 2. Start the proxy

```bash
mitmweb --listen-port 8080
```

This opens a web UI at http://127.0.0.1:8081 where you'll see captured traffic.

### 3. Configure your phone to use the proxy

1. Find your computer's local IP address:
   - macOS: `ipconfig getifaddr en0`
   - Linux: `hostname -I | awk '{print $1}'`
   - Windows: `ipconfig` → look for IPv4 Address
2. On your phone, go to **Wi-Fi settings** → tap your connected network
3. Set **HTTP Proxy** to **Manual**:
   - Server: `<your computer's IP>`
   - Port: `8080`

### 4. Install the mitmproxy CA certificate

This is required to decrypt HTTPS traffic from the Nanit app.

1. On your phone's browser, navigate to: **http://mitm.it**
2. Download and install the certificate for your OS (iOS or Android)

**iOS:**
- Tap the iOS link to download the profile
- Go to **Settings → General → VPN & Device Management** → install the profile
- Go to **Settings → General → About → Certificate Trust Settings** → enable "mitmproxy"

**Android:**
- Download the `.cer` file
- Go to **Settings → Security → Install a certificate → CA certificate**
- Select the downloaded file

### 5. Capture the Care Logs traffic

With the proxy running and phone configured:

1. **Open the Nanit app** on your phone
2. Navigate to the **Activity** or **Care Logs** tab
3. **Scroll through existing entries** (this triggers GET requests)
4. **Add a new feeding** (bottle or nursing) — this triggers POST requests
5. **Add a new diaper change** — another POST request
6. **Delete an entry** if possible — might trigger DELETE request

### 6. Export the results

In the mitmweb UI (http://127.0.0.1:8081):

1. Filter flows by domain: type `~d api.nanit.com` in the filter bar
2. Look for requests to paths containing words like:
   - `activities`, `care_logs`, `logs`, `feedings`, `diapers`, `timeline`
   - Any path under `/babies/{uid}/` that isn't `/messages` or `/snapshot`
3. For each interesting request, click it and note:
   - **Full URL** (method + path + query params)
   - **Request headers** (especially any special ones beyond Authorization)
   - **Request body** (for POST/PUT requests)
   - **Response body** (the JSON data structure)

**To export all flows:**
```bash
# In terminal where mitmproxy is running, or use:
mitmdump -r ~/.mitmproxy/flows -w nanit-capture.txt --set flow_detail=3
```

Or simply **screenshot** each interesting request/response from the mitmweb UI.

### 7. Clean up

1. Remove the proxy setting from your phone's Wi-Fi
2. (Optional) Remove the CA certificate from your phone
3. Stop mitmproxy with Ctrl+C

---

## Option B: Proxyman (macOS only, GUI, easier)

If the tester uses macOS and prefers a GUI:

1. Download [Proxyman](https://proxyman.io/) (free tier works)
2. Follow their [iOS setup guide](https://docs.proxyman.io/debug-devices/ios-device)
3. In Proxyman, right-click `api.nanit.com` → **Enable SSL Proxying**
4. Perform the same app actions as Step 5 above
5. Export/screenshot the captured requests

---

## What We Need From You

Please share the following for **each new endpoint** you find:

```
## Endpoint: [name]

**Request:**
- Method: GET/POST/PUT/DELETE
- URL: https://api.nanit.com/babies/{baby_uid}/...
- Query params: ?limit=20&...
- Body (if POST/PUT): { ... }

**Response:**
- Status: 200
- Body: { ... }
```

**Critical data points:**
- The exact URL path (e.g., `/babies/{uid}/activities` or `/babies/{uid}/care_logs`)
- The JSON structure of the response (field names, data types)
- Whether pagination exists (look for `next_page`, `offset`, `cursor` fields)
- What fields a feeding entry contains (amount, unit, type, timestamp, etc.)
- What fields a diaper entry contains (type, timestamp, notes, etc.)

---

## Alternative: Run the Probe Script First

Before doing the full proxy intercept, you can run our automated probe script which tries common endpoint patterns:

```bash
# From the ha-nanit repo root:
just login              # authenticate first
python3 tools/nanit-activities.py --verbose
```

If any endpoints return **403 Forbidden** instead of 404, that confirms they exist but are subscription-gated. If you have the Insights plan and they return 200, we might not need the proxy intercept at all!

---

## Sharing Results

Post your findings in [Issue #49](https://github.com/wealthystudent/ha-nanit/issues/49) or share directly with the maintainer. Even partial results (e.g., "I found the endpoint but the response is complex") are valuable — we can iterate from there.

**Security note:** Redact your `access_token` and any personal data (baby names, UIDs) before sharing publicly. Replace them with placeholders like `{baby_uid}` and `{token}`.
