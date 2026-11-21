"""Fixtures for the tests that use a real recorder database.

The recorder's database fixture must be set up before Home Assistant's
``hass`` fixture, and it asserts that. The autouse fixture in the parent
conftest requests ``enable_custom_integrations``, which requests ``hass``,
so a test that asks for ``recorder_mock`` directly fails in
``recorder_db_url``. Home Assistant's test plugin offers
``mock_recorder_before_hass`` for this: ``hass`` waits for it, and
overriding it here to depend on ``async_test_recorder`` makes the
database ready first. Only the tests in this package get a recorder.

Each test then asks for ``recorder_mock``, an in-memory SQLite database by
default; ``@pytest.mark.parametrize("persistent_database", [True])`` puts it
in a file instead.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def mock_recorder_before_hass(async_test_recorder: object) -> None:
    """Prepare the recorder's database before Home Assistant starts."""
