# Security Audit Checklist for Home Assistant Integrations

This document is the **mandatory reference** for security reviews of all code changes in this repository.
Every PR, issue fix, and release must be evaluated against these vulnerability classes before merge.

Use this as a checklist: for each category, verify the change does not introduce or worsen the described vulnerability pattern.

> **Scope**: This covers both the HA integration (`custom_components/nanit/`) and the client library (`packages/aionanit/`).

---

## How to Use This Document

**For PR reviewers (human or AI agent):**

1. Identify which files the PR touches.
2. Map those files to the relevant sections below using the [File → Section Map](#file-to-section-map).
3. For each relevant section, verify the change against every checklist item.
4. Document findings as PR comments. Block merge if any Critical or High item fails.

**For release validation:**

Run through the full checklist against all changes since the last release tag.

---

## File-to-Section Map

| Files changed | Relevant sections |
|---|---|
| `config_flow.py`, `strings.json` | [1. Auth](#1-authentication--authorization), [2. Credentials](#2-credential-storage--leakage), [3. Input Validation](#3-input-validation--injection), [12. Info Disclosure](#12-information-disclosure) |
| `hub.py`, `__init__.py` | [2. Credentials](#2-credential-storage--leakage), [8. Privilege Escalation](#8-privilege-escalation-within-ha-runtime), [16. Async Safety](#16-async--concurrency-safety) |
| `coordinator.py` | [5. Network](#5-network-security), [8. Privilege Escalation](#8-privilege-escalation-within-ha-runtime), [16. Async Safety](#16-async--concurrency-safety), [20. aiohttp Traps](#20-aiohttp-api-migration-traps) |
| `camera.py` (HA entity) | [5. Network](#5-network-security), [7. XSS](#7-cross-site-scripting-xss), [13. Media/Streaming](#13-media--streaming-security), [21. Subprocess Injection](#21-subprocess-argument-injection-media-pipelines) |
| `sensor.py`, `binary_sensor.py`, `switch.py`, `number.py`, `light.py`, `select.py` | [7. XSS](#7-cross-site-scripting-xss), [8. Privilege Escalation](#8-privilege-escalation-within-ha-runtime) |
| `sanitize.py` | [3. Input Validation](#3-input-validation--injection), [7. XSS](#7-cross-site-scripting-xss) |
| `entity.py` | [7. XSS](#7-cross-site-scripting-xss), [8. Privilege Escalation](#8-privilege-escalation-within-ha-runtime) |
| `diagnostics.py` | [2. Credentials](#2-credential-storage--leakage) |
| `manifest.json` | [10. Supply Chain](#10-supply-chain--dependency-risks), [11. Manifest Security](#11-manifest-security) |
| `aionanit/auth.py` | [1. Auth](#1-authentication--authorization), [2. Credentials](#2-credential-storage--leakage), [5. Network](#5-network-security), [22. Token Integrity](#22-token-integrity--cryptographic-weaknesses) |
| `aionanit/rest.py` | [3. Input Validation](#3-input-validation--injection), [5. Network](#5-network-security), [12. Info Disclosure](#12-information-disclosure), [20. aiohttp Traps](#20-aiohttp-api-migration-traps) |
| `aionanit/camera.py` | [5. Network](#5-network-security), [13. Media/Streaming](#13-media--streaming-security), [14. Protobuf/Deserialization](#14-deserialization--protobuf-security), [16. Async Safety](#16-async--concurrency-safety), [23. WebSocket Integrity](#23-websocket-backend-integrity) |
| `aionanit/ws/transport.py` | [5. Network](#5-network-security), [16. Async Safety](#16-async--concurrency-safety), [20. aiohttp Traps](#20-aiohttp-api-migration-traps), [23. WebSocket Integrity](#23-websocket-backend-integrity) |
| `aionanit/ws/protocol.py` | [14. Protobuf/Deserialization](#14-deserialization--protobuf-security) |
| `aionanit/parsers.py` | [14. Protobuf/Deserialization](#14-deserialization--protobuf-security) |
| `aionanit/models.py` | [7. XSS](#7-cross-site-scripting-xss) |
| `.github/workflows/*` | [10. Supply Chain](#10-supply-chain--dependency-risks), [17. CI/CD Security](#17-cicd-pipeline-security) |
| `tests/*` | [18. Test Security](#18-test-security) |

---

## 1. Authentication & Authorization

### 1.1 — OAuth2 / Token Flow Manipulation
- [ ] Auth flows validate redirect URIs against an allowlist (no open redirects)
- [ ] Token exchange endpoints verify `state` parameter to prevent CSRF
- [ ] No `javascript:`, `data:`, or custom scheme URIs accepted in auth callbacks
- [ ] PKCE used where supported by the upstream API

**HA CVE reference**: CVE-2023-41893 (open redirect_uri), CVE-2023-41895 (javascript: URI → account takeover)

### 1.2 — Reauth Flow Credential Integrity
- [ ] Reauth step verifies new credentials belong to the same account as the original config entry
- [ ] Reauth does not allow credential swap (replacing account A's tokens with account B's)

### 1.3 — Long-Lived Access Token Handling
- [ ] Long-lived tokens are never logged, even at debug level
- [ ] Tokens are stored in `entry.data` (encrypted), never in `entry.options` (plaintext)
- [ ] Token values are not included in URLs, query strings, or referrer-leaking contexts

### 1.4 — MFA Bypass
- [ ] MFA enforcement cannot be bypassed by replaying a previous session token
- [ ] MFA state is not cached in a way that allows skipping verification on reauth

### 1.5 — Session Fixation
- [ ] Session/token values are regenerated after successful authentication (not reused from pre-auth state)
- [ ] No predictable token generation patterns

**ha-nanit specific**: Config flow handles Nanit credentials + MFA. Verify `config_flow.py` reauth flow validates account identity.

---

## 2. Credential Storage & Leakage

### 2.1 — Plaintext Credential Storage
- [ ] All secrets (passwords, tokens, API keys) stored in `entry.data`, never in `entry.options`
- [ ] No credentials written to YAML configuration files
- [ ] No credentials hardcoded in source code

### 2.2 — Credential Logging
- [ ] No `_LOGGER.debug("Config: %s", user_input)` or equivalent that dumps full input dicts
- [ ] Log statements only reference non-sensitive fields (host, camera name, etc.)
- [ ] Exception handlers don't log request/response bodies containing tokens
- [ ] WebSocket frame logging does not include auth headers

### 2.3 — Diagnostics Redaction
- [ ] `async_get_config_entry_diagnostics()` uses `async_redact_data()` with explicit redaction set
- [ ] Redaction set includes: passwords, tokens, access_token, refresh_token, api_key, email, and any Nanit-specific secrets
- [ ] Nested data structures are redacted (not just top-level keys)

### 2.4 — Token in URL / Stream URL Leakage
- [ ] RTMPS stream URLs containing access tokens are not logged
- [ ] Stream URLs are not stored in entity attributes visible in the HA frontend
- [ ] Token-bearing URLs are not included in error messages

**ha-nanit specific**: `camera.stream_source()` returns RTMPS URL with embedded access token. `diagnostics.py` must redact all token fields. `TokenManager` callback must not leak tokens.

---

## 3. Input Validation & Injection

### 3.1 — Config Flow Input Validation
- [ ] All user input from config/options flow validated with voluptuous schema
- [ ] Host/IP fields validated for format (no arbitrary strings accepted as hostnames)
- [ ] Port fields validated for range (1-65535)
- [ ] URL fields validated for scheme (https only for cloud, http/https for local)
- [ ] No raw `user_input` dict passed directly to API calls or stored without validation

### 3.2 — Jinja2 Template Injection
- [ ] No entity state values derived from external API data are used in Jinja2 templates without sanitization
- [ ] Integration does not render Jinja2 templates from user/API-controlled input
- [ ] If integration serves HTTP views: `jinja2.Environment(autoescape=True)` is used

### 3.3 — Shell Command Injection
- [ ] No use of `os.system()`, `subprocess.run(shell=True)`, or `asyncio.create_subprocess_shell()` with any variable input
- [ ] If subprocess is needed: use `subprocess.run([...], shell=False)` with explicit argument list

### 3.4 — YAML Deserialization
- [ ] Any YAML parsing uses `yaml.safe_load()` or `Loader=SafeLoader`, never bare `yaml.load()`
- [ ] Third-party dependencies audited for unsafe YAML loading

### 3.5 — SQL / NoSQL Injection
- [ ] If any database interaction exists: parameterized queries only, no string concatenation

### 3.6 — Server-Side Template Injection (SSTI)
- [ ] No `hass.async_render()` or `Template().async_render()` called with externally-sourced template strings
- [ ] Template rendering only uses integration-controlled template strings, never API response data

**ha-nanit specific**: Config flow accepts camera IPs in options flow. Validate IP format strictly. API response data (baby names, camera names) flows into entity attributes — sanitize before use.

---

## 4. Path Traversal & Filesystem Access

### 4.1 — `os.path.join()` with User Input
- [ ] No `os.path.join(base, user_input)` where `user_input` could be absolute or contain `../`
- [ ] All file paths derived from user/API input are validated with `pathlib.Path.resolve()` and checked to be under the expected base directory
- [ ] `os.path.isabs()` check before any path join with external input

**HA CVE reference**: CVE-2025-65713 (Downloader path traversal → RCE), CVE-2021-3152 (HACS path traversal → credential theft)

### 4.2 — Static File Serving
- [ ] If integration registers HTTP routes that serve files: path is canonicalized and bounded
- [ ] No direct passthrough of URL path segments to filesystem reads

### 4.3 — `hass.config.path()` Escape
- [ ] If `hass.config.path()` is used with any variable input: resolved path verified to be under `config_dir`

---

## 5. Network Security

### 5.1 — TLS Certificate Validation
- [ ] Cloud API calls use `async_get_clientsession(hass)` with default SSL verification
- [ ] `verify_ssl=False` / `ssl=False` / `ssl.CERT_NONE` only used for documented local device connections with self-signed certificates
- [ ] Any SSL bypass is explicitly documented with justification

### 5.2 — Server-Side Request Forgery (SSRF)
- [ ] User-supplied URLs (from config flow or options) are validated before server-side requests
- [ ] Private/link-local IP ranges blocked for cloud-targeting URLs (169.254.x.x, 10.x.x.x, 172.16-31.x.x, 192.168.x.x, 127.x.x.x)
- [ ] No open redirect patterns that could be chained into SSRF

**HA CVE reference**: CVE-2023-41899 (SSRF via hassio.addon_stdin), CVE-2026-32111 (ha-mcp SSRF)

### 5.3 — DNS Rebinding
- [ ] If integration serves web UI: Host header validation in place
- [ ] Local API endpoints validate request origin

### 5.4 — WebSocket Security
- [ ] WebSocket connections use TLS (wss://)
- [ ] WebSocket auth tokens are passed in headers, not URL query strings (to avoid server logs/referrer leakage)
- [ ] WebSocket message size limits enforced to prevent memory exhaustion
- [ ] Reconnect logic has exponential backoff (no infinite rapid retry loop)

### 5.5 — mDNS/SSDP Discovery Spoofing
- [ ] If integration uses `zeroconf`/`ssdp` discovery: discovered device identity is cryptographically verified before trusting
- [ ] Discovery data (IP, port, name) not blindly trusted for connection establishment

**ha-nanit specific**: Local camera connections use `ssl.CERT_NONE` on port 442 — this is a known tradeoff documented in AGENTS.md. Cloud connections to `api.nanit.com` and `media-secured.nanit.com` MUST use proper TLS. WebSocket transport (`ws/transport.py`) must enforce size limits and backoff.

---

## 6. Webhook Security

### 6.1 — Webhook Authentication
- [ ] Incoming webhook payloads verified with HMAC signature (e.g., `X-Hub-Signature-256`) before processing
- [ ] Webhook ID treated as sensitive (not logged, not in error messages)

### 6.2 — Webhook State Injection
- [ ] Webhook handlers do not allow unauthenticated entity state changes
- [ ] Webhook data validated against expected schema before use

### 6.3 — Local-Only Webhook Bypass
- [ ] `local_only` flag understood to be bypassable via Nabu Casa SniTun proxy (CVE-2023-41894)
- [ ] Webhooks requiring local-only access have additional authentication

**ha-nanit specific**: If the integration registers webhooks for camera events, verify payload authentication.

---

## 7. Cross-Site Scripting (XSS)

### 7.1 — Stored XSS via Entity/Device Names
- [ ] Entity `friendly_name`, device name, and all user-visible attributes sanitized before storage
- [ ] Data from external APIs (Nanit cloud: baby names, camera names) does not contain HTML/script tags
- [ ] If API data is used as entity names: strip or escape HTML entities
- [ ] All API-provided names pass through `sanitize_name()` (from `sanitize.py`) before use in `DeviceInfo`, `description_placeholders`, or `translation_placeholders`
- [ ] New entity platforms that display API-sourced names import and use `sanitize_name()` at the boundary

**HA CVE reference**: CVE-2025-62172 (Energy Dashboard XSS), CVE-2026-33044 (Map card XSS), CVE-2026-33045 (History-graph XSS)

### 7.2 — XSS via Persistent Notifications
- [ ] Persistent notifications created by the integration do not include unsanitized external data
- [ ] No Markdown links pointing to external URLs constructed from API data

### 7.3 — XSS in Custom Frontend Panels
- [ ] If integration registers custom frontend panels or cards: all dynamic content uses `textContent`, never `innerHTML`
- [ ] No user/API-controlled data interpolated into HTML template strings

**ha-nanit specific**: Baby/camera names from the Nanit API become entity names and device names. A compromised Nanit account or MITM could inject XSS payloads via these names. All such names MUST be sanitized via `sanitize.py:sanitize_name()` before passing to HA. Currently enforced in `entity.py` (device info), `config_flow.py` (description placeholders), and `hub.py` (translation placeholders).

---

## 8. Privilege Escalation Within HA Runtime

### 8.1 — Cross-Service Calls
- [ ] Integration does not call services outside its own domain unless explicitly required and documented
- [ ] No calls to `shell_command.*`, `python_script.*`, or `homeassistant.stop`

### 8.2 — Cross-Entity State Manipulation
- [ ] Integration only sets state on its own entities (never other integrations' entities)
- [ ] `hass.states.async_set()` only used for entities the integration owns

### 8.3 — Event Bus Injection
- [ ] Integration only fires events in its own namespace
- [ ] No firing of `call_service`, `homeassistant_start`, or other system events

### 8.4 — Service Registration Namespace
- [ ] Services registered under the integration's own domain only
- [ ] No overwriting of existing services from other integrations

### 8.5 — Direct Access to HA Internals
- [ ] No access to `hass.auth`, `hass.auth._store`, or internal auth APIs
- [ ] No direct reading of `.storage/` files or `secrets.yaml`
- [ ] No access to other integrations' config entry data

---

## 9. Code Execution Vectors

### 9.1 — Dangerous Functions
- [ ] No use of `eval()`, `exec()`, `compile()` with any variable input
- [ ] No use of `os.system()`, `subprocess.Popen(shell=True)`, `asyncio.create_subprocess_shell()`
- [ ] No use of `pickle.loads()`, `marshal.loads()`, or `shelve` with untrusted data
- [ ] No use of `__import__()` with variable input

### 9.2 — Dynamic Code Loading
- [ ] No `importlib.import_module()` with user/API-controlled module names
- [ ] No `getattr()` chains with external input that could reach dangerous methods

### 9.3 — RestrictedPython / Sandbox Escapes
- [ ] If integration uses any sandboxed execution: verify sandbox cannot be escaped via `__class__.__mro__` chains

---

## 10. Supply Chain & Dependency Risks

### 10.1 — Requirement Pinning
- [ ] All `requirements` in `manifest.json` use exact version pinning (`==`) not range specifiers (`>=`)
- [ ] No git URL requirements (`git+https://...`) without commit SHA pinning
- [ ] Dependencies audited for known CVEs (run `pip-audit` or equivalent)

### 10.2 — Typosquatting / Package Hijack
- [ ] Package names in `requirements` verified against the canonical PyPI listing
- [ ] No recently-created or low-download-count packages without manual review
- [ ] Transitive dependencies checked for known vulnerabilities

### 10.3 — PyPI `.pth` File Attack
- [ ] Dependencies do not include `.pth` files that execute code at Python interpreter startup
- [ ] New dependencies manually reviewed for suspicious `setup.py` / `pyproject.toml` hooks

### 10.4 — Dependency Update Review
- [ ] Dependency version bumps treated as security-relevant changes (diff the upstream changelog)
- [ ] Major version bumps require full dependency audit

**ha-nanit specific**: `manifest.json` currently uses `"aionanit>=1.0.13"` — this is a range specifier. For maximum supply chain security, consider exact pinning (`==`). Since `aionanit` is maintained in-repo (`packages/aionanit/`), the risk is lower but still present for PyPI-published versions.

---

## 11. Manifest Security

### 11.1 — `iot_class` Accuracy
- [ ] `iot_class` accurately reflects the integration's network behavior
- [ ] `cloud_push` / `cloud_polling` integrations only contact documented endpoints

### 11.2 — Dependencies Declaration
- [ ] All `homeassistant.components.*` imports declared in manifest `dependencies` or `after_dependencies`
- [ ] No undeclared dependencies on privileged integrations (`http`, `websocket_api`, `supervisor`)

### 11.3 — `codeowners` Accuracy
- [ ] `codeowners` field lists actual maintainers (for security contact)

---

## 12. Information Disclosure

### 12.1 — Error Message Exposure
- [ ] Error handlers do not return stack traces, file paths, or internal IPs in HTTP responses
- [ ] Exception messages logged at appropriate level (not exposing secrets at WARNING/ERROR)

### 12.2 — Entity Attribute Leakage
- [ ] Sensitive data (tokens, passwords, internal IPs) not exposed as entity attributes
- [ ] Entity `extra_state_attributes` does not include raw API response data without filtering

### 12.3 — Debug Mode Exposure
- [ ] No debug endpoints or verbose modes left enabled in production code
- [ ] Debug logging behind `_LOGGER.debug()` — never at INFO/WARNING level for sensitive operations

### 12.4 — User Enumeration
- [ ] Config flow error messages don't distinguish between "user not found" and "wrong password"

---

## 13. Media & Streaming Security

### 13.1 — RTSP/RTMPS URL Injection
- [ ] Stream source URLs constructed from trusted data only (not user input or unvalidated API responses)
- [ ] No string concatenation of user input into stream URLs
- [ ] Token in stream URL is short-lived and scoped to the specific stream

### 13.2 — Media URL Token Leakage
- [ ] Stream URLs with embedded tokens not logged
- [ ] Stream URLs not cached in browser-accessible locations
- [ ] Token-bearing URLs expire within a reasonable timeframe

### 13.3 — Snapshot/Image Handling
- [ ] Image data from camera validated (magic bytes check) before processing
- [ ] No path traversal in snapshot storage paths
- [ ] Snapshot endpoints require authentication

**ha-nanit specific**: `camera.stream_source()` returns RTMPS URL with access token from `media-secured.nanit.com`. Verify token is short-lived and not logged. Snapshot fetching in `aionanit/rest.py` must validate response content-type.

---

## 14. Deserialization & Protobuf Security

### 14.1 — Protobuf Parsing Safety
- [ ] Protobuf messages from the camera/cloud validated against expected message types
- [ ] Malformed protobuf data handled gracefully (no crash, no infinite loop)
- [ ] Protobuf field size limits enforced (prevent memory exhaustion from oversized messages)
- [ ] Unknown fields ignored safely (don't trigger unexpected code paths)

### 14.2 — Unsafe Deserialization
- [ ] No `pickle.loads()`, `marshal.loads()`, `yaml.unsafe_load()` with data from network sources
- [ ] JSON deserialization used for all untrusted structured data
- [ ] No custom deserialization that could trigger code execution

### 14.3 — Protobuf-to-Model Parsing
- [ ] Parser functions (`parsers.py`) handle missing/null fields without crashing
- [ ] Numeric fields validated for expected ranges (no negative durations, no overflow)
- [ ] Brightness, volume, and percentage fields clamped to their documented range (e.g. 0-100) at parse time and before sending
- [ ] String fields from protobuf not used directly in file paths, commands, or templates

**ha-nanit specific**: `aionanit/proto/nanit_pb2.py` (generated), `aionanit/ws/protocol.py` (encode/decode), `aionanit/parsers.py` (protobuf → model). Camera sends protobuf over WebSocket — this is the primary untrusted deserialization surface. Numeric fields (`night_light_brightness`, `volume`) are clamped in both `parsers.py` and `camera.py`.

---

## 15. Operational & Deployment Security

### 15.1 — Backup Credential Exposure
- [ ] Documentation warns users that HA backups contain integration credentials
- [ ] No additional credential storage outside of HA's standard `.storage/` system

### 15.2 — Rate Limiting
- [ ] External API calls rate-limited to prevent account lockout or upstream abuse
- [ ] Reconnect/retry loops use exponential backoff with jitter
- [ ] No infinite retry loops that could consume system resources

### 15.3 — Resource Exhaustion
- [ ] WebSocket message buffers bounded (no unbounded memory growth)
- [ ] Polling coordinators have reasonable intervals (not sub-second)
- [ ] Background tasks properly cancelled on unload (`async_unload_entry`)

### 15.4 — Graceful Degradation
- [ ] API failures don't crash the integration (proper exception handling)
- [ ] Network timeouts configured on all HTTP/WS connections
- [ ] Unavailable state set on communication failure (not stale data)

---

## 16. Async & Concurrency Safety

### 16.1 — Race Conditions
- [ ] Shared mutable state protected by locks or atomic operations
- [ ] No TOCTOU (time-of-check-time-of-use) bugs in auth token refresh
- [ ] Config entry reload does not race with in-flight requests

### 16.2 — Blocking I/O in Event Loop
- [ ] No blocking calls (`requests`, `open()`, `time.sleep()`) in async code
- [ ] All I/O through `aiohttp`, `asyncio`, or `hass.async_add_executor_job()`

### 16.3 — Task Cleanup
- [ ] All `asyncio.Task` objects tracked and cancelled in `async_unload_entry` / `async_stop()`
- [ ] No orphaned tasks after config entry reload
- [ ] Background tasks follow `_start_*` / `_cancel_*` pattern (per AGENTS.md)

### 16.4 — Deadlock Prevention
- [ ] No `await` inside locked critical sections that could deadlock
- [ ] Lock ordering consistent if multiple locks used
- [ ] Timeout on all lock acquisitions

**ha-nanit specific**: `NanitCamera` manages multiple background tasks (token refresh, WebSocket keepalive). `NanitPushCoordinator` has availability grace period timer. Verify all tasks properly cancelled on unload and no shared state races.

---

## 17. CI/CD Pipeline Security

### 17.1 — GitHub Actions Security
- [ ] No `${{ github.event.pull_request.title }}` or similar user-controlled expressions in `run:` blocks
- [ ] Secrets not exposed in workflow logs
- [ ] Third-party actions pinned to commit SHA (not tags)
- [ ] `pull_request_target` not used with checkout of PR code (combined = RCE)

### 17.2 — PyPI Publishing Security
- [ ] Publishing workflow uses OIDC trusted publishing (not long-lived API tokens)
- [ ] Publishing only triggered from protected branches
- [ ] Build artifacts not influenced by PR content

**ha-nanit specific**: `.github/workflows/ci.yaml` and `publish-aionanit.yaml` — verify action pins and secret handling.

---

## 18. Test Security

### 18.1 — No Real Credentials in Tests
- [ ] Test fixtures use fake/mock credentials, never real API keys or tokens
- [ ] No `.env` files or credential fixtures committed to the repo
- [ ] Test configuration does not connect to real Nanit servers

### 18.2 — Test Coverage for Security Paths
- [ ] Auth flow error paths tested (wrong password, expired token, revoked access)
- [ ] Input validation rejection tested (invalid IP, oversized input, special characters)
- [ ] Malformed API response handling tested (missing fields, wrong types, oversized payloads)

---

## 19. Clickjacking & UI Framing

### 19.1 — Clickjacking via Missing Frame Protection
- [ ] If integration serves any HTTP views: `X-Frame-Options: DENY` or `Content-Security-Policy: frame-ancestors 'none'` header set
- [ ] No integration-served pages can be embedded in attacker-controlled iframes
- [ ] User-facing actions (install, configure, approve) cannot be triggered via transparent iframe overlay

**HA CVE reference**: CVE-2023-41897 (missing X-Frame-Options → clickjacking → malicious add-on install)

### 19.2 — OAuth Consent Clickjacking
- [ ] OAuth consent screens cannot be framed by external sites
- [ ] Token-granting actions require visible, un-frameable user interaction

---

## 20. aiohttp API Migration Traps

### 20.1 — `ssl=True` Semantic Inversion
- [ ] No use of `ssl=True` in aiohttp calls (this does NOT enable verification — it means "use provided SSL object")
- [ ] Use `ssl=None` (default, enables standard verification) or explicit `ssl=ssl.create_default_context()`
- [ ] When migrating from deprecated `verify_ssl` parameter: verify the replacement actually enables certificate checking

**HA CVE reference**: CVE-2025-25305 — dozens of integrations silently disabled TLS verification via this migration anti-pattern.

### 20.2 — aiohttp Connection Pool Race Conditions
- [ ] Shared `aiohttp.ClientSession` usage accounts for concurrent access (TOC-TOU in connection limit checks)
- [ ] No assumption that connection count checks are atomic under concurrent coroutines
- [ ] File responses (if any) account for stat/open race between check and use

---

## 21. Subprocess Argument Injection (Media Pipelines)

### 21.1 — FFmpeg / Media Tool Argument Injection
- [ ] Stream URLs passed to `ffmpeg` or media processing tools use argument-list form (never shell string splitting)
- [ ] Credentials embedded in RTSP/RTMPS URLs do not contain characters that could be interpreted as argument separators (spaces, quotes, dashes)
- [ ] If stream URLs contain user-configurable fields: escape or quote before passing to subprocess

**Reference**: TALOS-2018-0539 (Samsung SmartThings — camera password with spaces caused ffmpeg argument injection)

### 21.2 — URL-to-Subprocess Sanitization
- [ ] No media URL is passed to a subprocess via string interpolation or f-string
- [ ] Subprocess calls use explicit argument lists: `['/usr/bin/ffmpeg', '-i', url]` not `f'ffmpeg -i {url}'`

**ha-nanit specific**: RTMPS stream URLs from `media-secured.nanit.com` are passed to HA's stream integration which uses ffmpeg. Verify the URL construction cannot inject ffmpeg arguments.

---

## 22. Token Integrity & Cryptographic Weaknesses

### 22.1 — Unsigned / Stateless Token Forgery
- [ ] Any tokens issued or consumed by the integration are cryptographically signed (HMAC, JWT RS256, etc.)
- [ ] Tokens are not plain base64-encoded JSON without signature verification
- [ ] Token contents (especially target URLs, scopes, or permissions) cannot be modified by the bearer

**HA CVE reference**: CVE-2026-32111 (ha-mcp — unsigned base64 JSON tokens allowed `ha_url` field modification → SSRF)

### 22.2 — Weak Key Derivation
- [ ] Any encryption keys derived from user passphrases use memory-hard KDFs (Argon2id, scrypt) not PBKDF2/SHA
- [ ] Key derivation parameters meet current recommendations (Argon2id: ≥64MB memory, ≥3 iterations)

### 22.3 — Cryptographic Downgrade
- [ ] No silent fallback to weaker cryptographic algorithms or protocols on error
- [ ] Format version negotiation fails closed (reject unknown/corrupted headers, don't fallback to legacy)

**Reference**: Trail of Bits audit of HA SecureTar v3 (2026) — corrupt header caused silent fallback to AES-128 with non-memory-hard KDF.

---

## 23. WebSocket Backend Integrity

### 23.1 — WebSocket Connection Target Validation
- [ ] WebSocket connection targets (URLs) are not derived from user-controlled parameters (query strings, state params, redirects)
- [ ] OAuth `state` parameter does not contain the backend WebSocket URL
- [ ] Backend URL is hardcoded or derived from trusted configuration only

**HA CVE reference**: CVE-2023-41896 (auth_callback state poisoning → fake WebSocket server → account takeover)

### 23.2 — WebSocket Origin Verification
- [ ] WebSocket server validates `Origin` header to prevent cross-origin connections
- [ ] No WebSocket endpoint accepts connections from arbitrary origins

**ha-nanit specific**: `WsTransport` connects to camera WebSocket endpoints. Verify the target URL for both local (`wss://{camera_ip}:442`) and cloud (`wss://api.nanit.com/focus/cameras/{uid}/user_connect`) connections is derived from trusted sources only (config entry data + API responses over verified TLS), not from any user-controlled input.

---

## 24. Reverse Proxy & Network Boundary Confusion

### 24.1 — Proxy IP Spoofing / Locality Bypass
- [ ] If integration makes access control decisions based on source IP: account for reverse proxy scenarios (X-Forwarded-For, X-Real-IP headers can be spoofed)
- [ ] "Local only" restrictions not solely based on IP address matching
- [ ] Cloud tunnel / NAT traversal services may make remote requests appear local

**HA CVE reference**: CVE-2023-41894 (SniTun proxy set source=127.0.0.1 for all cloud-proxied requests, bypassing local-only webhook restrictions)

### 24.2 — Docker Network Isolation Assumptions
- [ ] No assumption that Docker bridge interfaces (172.30.32.x) are unreachable from the physical LAN
- [ ] Internal service endpoints bound to bridge interfaces must still require authentication
- [ ] Host-network-mode containers are reachable from any network interface on the host

**HA CVE reference**: CVE-2026-34205 (CVSS 9.6 — add-on management APIs on Docker bridge reachable from LAN)

---

## Known Accepted Risks

Document any intentional security tradeoffs here with justification:

| Risk | Justification | Mitigation |
|---|---|---|
| `ssl.CERT_NONE` for local camera connections (port 442) | Nanit cameras use self-signed TLS certificates. No CA-signed option available. | Connection is LAN-only. IP must be explicitly configured by user in options flow. |
| `aionanit>=1.0.13` range pinning in manifest.json | Library is maintained in-repo and published to PyPI by the same maintainers. | Consider switching to exact pinning for releases. Monitor PyPI for unauthorized publishes. |

---

## CVE Quick Reference

Key CVEs that inform this checklist (see full details in the vulnerability sections above):

| CVE | Severity | Category | Summary |
|---|---|---|---|
| CVE-2023-27482 | Critical (10.0) | Auth bypass | Supervisor API double URL-encoding path traversal |
| CVE-2023-41895 | Critical | Auth/XSS | javascript: URI in auth callback → account takeover |
| CVE-2023-41896 | Critical | WS integrity | Fake WebSocket server via auth_callback state poisoning |
| CVE-2026-34205 | Critical (9.6) | Network boundary | Unauthenticated add-on APIs via Docker host network |
| CVE-2024-27081 | Critical | Path traversal | ESPHome config traversal → firmware poisoning → RCE |
| CVE-2025-62172 | High (8.5) | XSS | Stored XSS via entity friendly_name in dashboard |
| CVE-2025-65713 | High | Path traversal | Downloader integration arbitrary file write → RCE |
| CVE-2021-3152 | High | Path traversal | HACS path traversal → credential theft |
| CVE-2025-25305 | High (7.0) | aiohttp trap | SSL/TLS silently disabled via `ssl=True` migration anti-pattern |
| CVE-2026-32111 | High | Token integrity | Unsigned stateless tokens → SSRF via `ha_url` modification |
| CVE-2023-41897 | Medium-High | Clickjacking | Missing X-Frame-Options → iframe UI redressing → add-on install |
| CVE-2023-41894 | Medium | Proxy confusion | Local-only webhooks accessible via Nabu Casa SniTun proxy |
| CVE-2023-41899 | Medium | SSRF | Partial SSRF via hassio.addon_stdin |
| CVE-2023-50715 | Medium | Info disclosure | Login page user enumeration |
| CVE-2026-33044 | Medium | XSS | Stored XSS via device name in Map card |

---

## References

| Source | Link |
|---|---|
| HA Official Security Page | https://www.home-assistant.io/security/ |
| HA Developer Docs | https://developers.home-assistant.io/ |
| elttam "PwnAssistant" | https://www.elttam.com/blog/pwnassistant/ |
| GitHub Security Lab Audit | https://github.blog/security/vulnerability-research/securing-our-home-labs-home-assistant-code-review/ |
| 2021 HACS Disclosure | https://www.home-assistant.io/blog/2021/01/22/security-disclosure/ |
| JFrog Revival Hijack | https://jfrog.com/blog/revival-hijack-pypi-hijack-technique-exploited-22k-packages-at-risk |
| HA Backup Encryption Blog | https://www.home-assistant.io/blog/2026/03/26/modernizing-encryption-of-home-assistant-backups/ |
| Talos SmartThings RTSP Injection | https://talosintel.com/vulnerability_reports/TALOS-2018-0539 |
| ESPHome Security Advisories | https://github.com/esphome/esphome/security/advisories |
| ha-mcp SSRF Advisory | https://github.com/homeassistant-ai/ha-mcp/security/advisories/GHSA-fmfg-9g7c-3vq7 |
