# Testing

## Unit tests (no hardware needed)

```bash
just test          # Integration tests (30 — config flow, migration, hub, lifecycle)
just test-lib      # aionanit library tests (183 — protocol, REST, auth, transport)
just test-all      # Both
```

First time setup: `pip install -r requirements-test.txt`

> **Note:** The two suites run separately (not together) due to a `tests/` namespace collision.

### What the integration tests cover

| File | # | Covers |
|------|---|--------|
| `test_config_flow.py` | 12 | Login, MFA, duplicate email abort, options flow (per-camera IP) |
| `test_init.py` | 9 | Setup/unload, auth/connection errors, v1→v2 migration (5 cases) |
| `test_hub.py` | 9 | Multi-baby discovery (1/3/0 babies), partial failure, token refresh |

Multi-camera scenarios are fully tested via mocks — no camera hardware required.

## Dev HA instance (Docker)

```bash
just dev           # Start → http://localhost:8123
just dev-logs      # Tail logs (debug logging for all custom_components)
just dev-restart   # Restart after code changes
just dev-stop      # Stop
just dev-reset     # Wipe all state for a fresh start
```

The entire `custom_components/` directory is mounted read-only — any custom component you put there is available in the dev HA. Edit source files normally, then `just dev-restart`.

State lives in `dev-config/` (gitignored except `configuration.yaml`).

## Feature-specific test guides

See `testing/docs/` for step-by-step instructions for testing specific features:

| Guide | Feature |
|-------|---------|
| [multi-camera.md](docs/multi-camera.md) | Multi-camera support (simulated + real) |

## Pre-release checklist

- [ ] `just test` → 30/30 pass
- [ ] `just test-lib` → 183/183 pass
- [ ] Docker dev instance: add integration, all cameras appear
- [ ] Docker dev instance: options flow works (set/clear camera IP)
- [ ] Docker dev instance: restart HA → cameras reconnect
- [ ] Docker dev instance: remove + re-add integration → clean cycle
