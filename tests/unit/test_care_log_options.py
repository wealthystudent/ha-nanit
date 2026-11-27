"""The care log privacy choice: whether the calendar shows notes and who logged them."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanit import _make_options_update_listener
from custom_components.nanit import config_flow as nanit_config_flow
from custom_components.nanit.const import (
    CONF_MFA_CODE,
    CONF_READ_ONLY,
    CONF_REFRESH_TOKEN,
    CONF_SHOW_CARE_LOG_NOTES,
    CONF_SPEAKER_IPS,
    DOMAIN,
    is_show_care_log_notes,
)
from custom_components.nanit.hub import NanitHub

from .conftest import (
    MOCK_EMAIL,
    MOCK_MFA_TOKEN,
    MOCK_PASSWORD,
    mock_entry_data_v2,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore::pytest.PytestRemovedIn9Warning"),
]

_CREDENTIALS = {CONF_EMAIL: MOCK_EMAIL, CONF_PASSWORD: MOCK_PASSWORD}

# An option an earlier build of this feature stored. Nothing reads it now.
_LEFTOVER = "import_care_log_statistics"


def _as_dict(result: Any) -> dict[str, Any]:
    return cast(dict[str, Any], result)


def _schema_defaults(result: Any) -> dict[str, Any]:
    """Return each form field's default, keyed by field name."""
    return {
        str(key): key.default()
        for key in _as_dict(result)["data_schema"].schema
        if callable(getattr(key, "default", None))
    }


# ---------------------------------------------------------------------------
# Reading the choice
# ---------------------------------------------------------------------------


def test_an_entry_without_the_key_hides_notes() -> None:
    """Notes and who logged each entry stay hidden unless switched on."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_READ_ONLY: True})

    assert is_show_care_log_notes(entry) is False


@pytest.mark.parametrize("value", [True, False])
def test_the_reader_returns_the_stored_choice(value: bool) -> None:
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SHOW_CARE_LOG_NOTES: value})

    assert is_show_care_log_notes(entry) is value


@pytest.mark.parametrize("value", ["true", 1, None])
def test_only_an_explicit_true_turns_the_choice_on(value: Any) -> None:
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SHOW_CARE_LOG_NOTES: value})

    assert is_show_care_log_notes(entry) is False


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------


async def test_sign_in_form_asks_nothing_about_care_logs(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})

    assert result.get("step_id") == "credentials"
    fields = {str(key) for key in _as_dict(result)["data_schema"].schema}
    assert CONF_SHOW_CARE_LOG_NOTES not in fields
    assert _LEFTOVER not in fields


async def test_new_sign_in_stores_nothing_about_care_logs(
    hass: HomeAssistant, mock_config_flow_client: MagicMock
) -> None:
    """A new entry hides notes through the default; nothing is stored for it."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _CREDENTIALS)

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.CREATE_ENTRY
    entry = result_data["result"]
    assert entry.options == {}
    assert CONF_SHOW_CARE_LOG_NOTES not in entry.data
    assert is_show_care_log_notes(entry) is False


async def test_a_sign_in_with_a_verification_code_stores_nothing_about_care_logs(
    hass: HomeAssistant, mock_config_flow_client: MagicMock
) -> None:
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitMfaRequiredError(
        mfa_token=MOCK_MFA_TOKEN
    )
    mock_config_flow_client.async_verify_mfa.side_effect = [
        nanit_config_flow.NanitAuthError("bad code"),
        {"access_token": "access", "refresh_token": "refresh"},
    ]

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**_CREDENTIALS, CONF_READ_ONLY: True}
    )
    assert result.get("step_id") == "mfa"
    # A wrong code first: the retry of the code step changes nothing either.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: "000000"}
    )
    assert result.get("step_id") == "mfa"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MFA_CODE: "123456"}
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.CREATE_ENTRY
    assert result_data["result"].options == {CONF_READ_ONLY: True}


@pytest.mark.parametrize(
    "error",
    [
        nanit_config_flow.NanitAuthError("bad password"),
        nanit_config_flow.NanitConnectionError("offline"),
    ],
)
async def test_a_failed_sign_in_asks_nothing_about_care_logs_and_the_retry_stores_nothing(
    hass: HomeAssistant, mock_config_flow_client: MagicMock, error: Exception
) -> None:
    mock_config_flow_client.async_login.side_effect = [
        error,
        {"access_token": "access", "refresh_token": "refresh"},
    ]

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _CREDENTIALS)
    assert result.get("step_id") == "credentials"
    assert _as_dict(result)["errors"]
    fields = {str(key) for key in _as_dict(result)["data_schema"].schema}
    assert CONF_SHOW_CARE_LOG_NOTES not in fields

    result = await hass.config_entries.flow.async_configure(result["flow_id"], _CREDENTIALS)

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.CREATE_ENTRY
    assert result_data["result"].options == {}


@pytest.mark.parametrize(
    "options",
    [
        {},
        {CONF_SHOW_CARE_LOG_NOTES: True},
        {CONF_SHOW_CARE_LOG_NOTES: False},
        {_LEFTOVER: True, CONF_SHOW_CARE_LOG_NOTES: True},
    ],
)
@pytest.mark.parametrize("with_code", [False, True])
async def test_re_authentication_keeps_the_choice(
    hass: HomeAssistant,
    mock_config_flow_client: MagicMock,
    options: dict[str, Any],
    with_code: bool,
) -> None:
    """Re-authentication changes tokens only. An entry without the key stays without it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ACCESS_TOKEN: "old", CONF_REFRESH_TOKEN: "old", CONF_EMAIL: MOCK_EMAIL},
        options=dict(options),
    )
    entry.add_to_hass(hass)
    if with_code:
        mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitMfaRequiredError(
            mfa_token=MOCK_MFA_TOKEN
        )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data
    )
    assert result.get("step_id") == "reauth_confirm"
    assert CONF_SHOW_CARE_LOG_NOTES not in _schema_defaults(result)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _CREDENTIALS)
    if with_code:
        assert result.get("step_id") == "reauth_mfa"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MFA_CODE: "123456"}
        )

    assert result.get("reason") == "reauth_successful"
    assert entry.data[CONF_ACCESS_TOKEN] != "old"
    assert dict(entry.options) == options


async def test_token_refresh_keeps_the_choice(hass: HomeAssistant) -> None:
    options = {CONF_SHOW_CARE_LOG_NOTES: False}
    entry = MockConfigEntry(
        domain=DOMAIN, data=mock_entry_data_v2(), version=2, unique_id=MOCK_EMAIL, options=options
    )
    entry.add_to_hass(hass)
    hub = NanitHub(hass, MagicMock(), entry)

    hub._on_tokens_refreshed("new_access", "new_refresh")

    assert entry.data[CONF_ACCESS_TOKEN] == "new_access"
    assert dict(entry.options) == options


# ---------------------------------------------------------------------------
# Options page
# ---------------------------------------------------------------------------


def _options_entry(hass: HomeAssistant, options: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, options=options)
    entry.runtime_data = SimpleNamespace(hub=SimpleNamespace(babies=[], speaker_uid_map={}))
    entry.add_to_hass(hass)
    return entry


async def _open_care_logs(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result.get("type") is FlowResultType.MENU
    assert "care_logs" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "care_logs"}
    )
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "care_logs"
    return result


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, False),
        ({CONF_SHOW_CARE_LOG_NOTES: False}, False),
        ({CONF_SHOW_CARE_LOG_NOTES: True}, True),
        ({_LEFTOVER: True, CONF_SHOW_CARE_LOG_NOTES: False}, False),
    ],
)
async def test_options_page_shows_the_current_choice(
    hass: HomeAssistant, options: dict[str, Any], expected: bool
) -> None:
    entry = _options_entry(hass, options)

    result = await _open_care_logs(hass, entry)

    assert _schema_defaults(result) == {CONF_SHOW_CARE_LOG_NOTES: expected}


@pytest.mark.parametrize("show_notes", [True, False])
async def test_options_page_saves_the_choice_explicitly_and_keeps_other_options(
    hass: HomeAssistant, show_notes: bool
) -> None:
    other = {
        CONF_READ_ONLY: True,
        CONF_SPEAKER_IPS: {"spk_1": "192.168.1.25"},
        _LEFTOVER: True,
        "future_option": "keep-me",
    }
    entry = _options_entry(hass, other)

    result = await _open_care_logs(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SHOW_CARE_LOG_NOTES: show_notes}
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.CREATE_ENTRY
    expected = {**other, CONF_SHOW_CARE_LOG_NOTES: show_notes}
    assert result_data["data"] == expected
    assert dict(entry.options) == expected


async def test_saving_a_changed_choice_reloads_through_the_options_listener(
    hass: HomeAssistant,
) -> None:
    entry = _options_entry(hass, {CONF_SHOW_CARE_LOG_NOTES: False})
    entry.add_update_listener(_make_options_update_listener(dict(entry.options)))

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        result = await _open_care_logs(hass, entry)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SHOW_CARE_LOG_NOTES: True}
        )
        await hass.async_block_till_done()

    reload.assert_awaited_once_with(entry.entry_id)
