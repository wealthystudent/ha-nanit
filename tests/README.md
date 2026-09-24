# Testing

## Layout

| Path | Covers |
|------|--------|
| `tests/unit/` | Integration tests (config flow, setup/migration, hub, entities, card registration, S&L, network recovery), plus `tools/release_notes.py`. Snapshots in `snapshots/`. 80% coverage gate. |
| `packages/aionanit/tests/` | aionanit library tests (auth, REST, protocol, transport, camera). Run without Home Assistant installed in CI. |

## Running tests (no hardware needed)

```bash
just test          # Integration tests with coverage
just test lib      # aionanit library tests
just test all      # Both
just test lib -k transport   # Extra args go to pytest
```

First time setup: `just setup`

## Dev HA instance (Docker)

```bash
just dev           # Start → http://localhost:8123
just dev logs      # Follow logs
just dev restart   # Restart after code changes
just dev stop      # Stop
just dev reset     # Wipe all state for a fresh start
```

The entire `custom_components/` directory is mounted read-only — any custom component you put there is available in the dev HA. Edit source files normally, then `just dev restart`.

State lives in the `ha-config` Docker volume; `just dev reset` deletes it.

## Manual test guides

Step-by-step instructions for testing specific features with the dev HA instance:

| Guide | Feature |
|-------|---------|
| [docs/testing-multi-camera.md](../docs/testing-multi-camera.md) | Multi-camera support (simulated + real) |

## Pre-release checklist

- [ ] `just check` passes
- [ ] Docker dev instance: add integration, all cameras appear
- [ ] Docker dev instance: options flow works (set/clear camera IP)
- [ ] Docker dev instance: restart HA → cameras reconnect
- [ ] Docker dev instance: remove + re-add integration → clean cycle
