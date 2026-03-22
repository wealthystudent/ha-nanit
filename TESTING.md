# Testing

## Unit tests (no hardware needed)

```bash
# Install test dependencies (once)
pip install -r requirements-test.txt

# Run integration tests (config flow, setup/unload, migration, hub)
python -m pytest tests/ -v

# Run aionanit library tests
python -m pytest packages/aionanit/tests/ -v

# Run everything
python -m pytest tests/ packages/aionanit/tests/ -v
```

### What the tests cover

| File | Tests | What it verifies |
|------|-------|-----------------|
| `test_config_flow.py` | 12 | Credentials, MFA, duplicate abort, options flow (camera IP) |
| `test_init.py` | 9 | Setup/unload lifecycle, v1→v2 migration (5 cases) |
| `test_hub.py` | 9 | Multi-baby discovery, partial failure, zero babies, token callback |

## Dev HA instance (Docker)

Runs a real HA with your `custom_components/nanit/` mounted read-only.

```bash
# Start
docker compose -f docker-compose.dev.yml up -d

# Open HA at http://localhost:8123

# View logs (nanit + aionanit debug logging enabled)
docker compose -f docker-compose.dev.yml logs -f homeassistant

# Restart after code changes
docker compose -f docker-compose.dev.yml restart homeassistant

# Stop
docker compose -f docker-compose.dev.yml down
```

HA config lives in `dev-config/`. The component is mounted read-only — edit source files normally, then restart the container.

### Testing multi-camera with one physical camera

Inject a fake second baby in `hub.py` (temporarily, for dev only):

```python
# In hub.py async_setup(), after: babies = await self._client.async_get_babies()
from aionanit.models import Baby
babies.append(Baby(uid="fake_baby_2", name="Test Baby 2", camera_uid="fake_cam_2"))
```

This creates a second device in HA. The real camera works normally; the fake one shows as unavailable. Verify: two device entries, independent entities, unload isolation.

Remove the hack before committing.
