from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
from collections.abc import Iterator
from typing import Any, cast

import pytest

from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_PASSWORD,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nanit import config_flow as nanit_config_flow

from custom_components.nanit.const import (
    CONF_CAMERA_IP,
    CONF_CAMERA_IPS,
    CONF_MFA_CODE,
    CONF_REFRESH_TOKEN,
    CONF_STORE_CREDENTIALS,
    DOMAIN,
)


def _as_dict(result: Any) -> dict[str, Any]:
    return cast(dict[str, Any], result)


async def _resolve_hass(hass: Any) -> HomeAssistant:
    if hasattr(hass, "__anext__"):
        return await hass.__anext__()
    return cast(HomeAssistant, hass)

from .conftest import (
    MOCK_ACCESS_TOKEN,
    MOCK_BABY_1,
    MOCK_BABY_2,
    MOCK_EMAIL,
    MOCK_MFA_TOKEN,
    MOCK_PASSWORD,
    MOCK_REFRESH_TOKEN,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.filterwarnings("ignore::pytest.PytestRemovedIn9Warning"),
]


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(
    enable_custom_integrations,
) -> Iterator[None]:
    yield


@pytest.fixture(autouse=True)
def _ensure_repo_on_syspath() -> Iterator[None]:
    repo_root = Path(__file__).resolve().parents[1]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    yield


async def test_async_step_user_redirects_to_credentials(hass: HomeAssistant) -> None:
    hass = await _resolve_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )

    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "credentials"


async def test_credentials_valid_login_creates_entry(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_STORE_CREDENTIALS: True,
        },
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.CREATE_ENTRY
    assert result_data["result"].version == 2
    assert result_data["result"].unique_id == MOCK_EMAIL
    assert result_data["data"][CONF_EMAIL] == MOCK_EMAIL
    assert result_data["data"][CONF_PASSWORD] == MOCK_PASSWORD


async def test_credentials_mfa_required_goes_to_mfa(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitMfaRequiredError(
        mfa_token=MOCK_MFA_TOKEN
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_STORE_CREDENTIALS: False,
        },
    )

    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "mfa"


async def test_credentials_invalid_auth_shows_error(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitAuthError(
        "bad credentials"
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_STORE_CREDENTIALS: False,
        },
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.FORM
    assert result_data.get("step_id") == "credentials"
    assert _as_dict(result_data.get("errors")).get("base") == "invalid_auth"


async def test_credentials_connection_error_shows_error(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitConnectionError(
        "offline"
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_STORE_CREDENTIALS: False,
        },
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.FORM
    assert result_data.get("step_id") == "credentials"
    assert _as_dict(result_data.get("errors")).get("base") == "cannot_connect"


async def test_credentials_unknown_error_shows_error(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    mock_config_flow_client.async_login.side_effect = Exception("boom")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_STORE_CREDENTIALS: False,
        },
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.FORM
    assert result_data.get("step_id") == "credentials"
    assert _as_dict(result_data.get("errors")).get("base") == "unknown"


async def test_mfa_valid_code_creates_entry(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitMfaRequiredError(
        mfa_token=MOCK_MFA_TOKEN
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_STORE_CREDENTIALS: True,
        },
    )
    assert result.get("step_id") == "mfa"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MFA_CODE: "123456"},
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.CREATE_ENTRY
    assert result_data["result"].unique_id == MOCK_EMAIL


async def test_mfa_invalid_code_shows_error(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitMfaRequiredError(
        mfa_token=MOCK_MFA_TOKEN
    )
    mock_config_flow_client.async_verify_mfa.side_effect = nanit_config_flow.NanitAuthError(
        "bad mfa"
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_STORE_CREDENTIALS: False,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MFA_CODE: "000000"},
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.FORM
    assert result_data.get("step_id") == "mfa"
    assert _as_dict(result_data.get("errors")).get("base") == "invalid_mfa_code"


async def test_mfa_connection_error_shows_error(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitMfaRequiredError(
        mfa_token=MOCK_MFA_TOKEN
    )
    mock_config_flow_client.async_verify_mfa.side_effect = (
        nanit_config_flow.NanitConnectionError("offline")
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_STORE_CREDENTIALS: False,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MFA_CODE: "000000"},
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.FORM
    assert result_data.get("step_id") == "mfa"
    assert _as_dict(result_data.get("errors")).get("base") == "cannot_connect"


async def test_mfa_unknown_error_shows_error(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitMfaRequiredError(
        mfa_token=MOCK_MFA_TOKEN
    )
    mock_config_flow_client.async_verify_mfa.side_effect = Exception("boom")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_STORE_CREDENTIALS: False,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MFA_CODE: "000000"},
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.FORM
    assert result_data.get("step_id") == "mfa"
    assert _as_dict(result_data.get("errors")).get("base") == "unknown"


async def test_duplicate_email_aborts(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    existing = MockConfigEntry(domain=DOMAIN, unique_id=MOCK_EMAIL)
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_STORE_CREDENTIALS: False,
        },
    )

    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "already_configured"


async def test_reauth_valid_login_success_updates_entry(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ACCESS_TOKEN: "old_access",
            CONF_REFRESH_TOKEN: "old_refresh",
            CONF_STORE_CREDENTIALS: True,
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: "old_password",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
        },
    )

    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "reauth_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == MOCK_ACCESS_TOKEN
    assert entry.data[CONF_REFRESH_TOKEN] == MOCK_REFRESH_TOKEN
    assert entry.data[CONF_EMAIL] == MOCK_EMAIL
    assert entry.data[CONF_PASSWORD] == MOCK_PASSWORD


async def test_reauth_mfa_success_updates_entry(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ACCESS_TOKEN: "old_access",
            CONF_REFRESH_TOKEN: "old_refresh",
            CONF_STORE_CREDENTIALS: False,
            CONF_EMAIL: MOCK_EMAIL,
        },
    )
    entry.add_to_hass(hass)
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitMfaRequiredError(
        mfa_token=MOCK_MFA_TOKEN
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
        },
    )
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "reauth_mfa"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MFA_CODE: "123456"},
    )

    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "reauth_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == MOCK_ACCESS_TOKEN
    assert entry.data[CONF_REFRESH_TOKEN] == MOCK_REFRESH_TOKEN
    assert entry.data[CONF_EMAIL] == MOCK_EMAIL
    assert CONF_PASSWORD not in entry.data


async def test_reauth_invalid_auth_shows_error(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ACCESS_TOKEN: "old_access",
            CONF_REFRESH_TOKEN: "old_refresh",
            CONF_STORE_CREDENTIALS: True,
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
        },
    )
    entry.add_to_hass(hass)
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitAuthError(
        "bad credentials"
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
        },
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.FORM
    assert result_data.get("step_id") == "reauth_confirm"
    assert _as_dict(result_data.get("errors")).get("base") == "invalid_auth"


async def test_reauth_connection_error_shows_error(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ACCESS_TOKEN: "old_access",
            CONF_REFRESH_TOKEN: "old_refresh",
            CONF_STORE_CREDENTIALS: True,
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
        },
    )
    entry.add_to_hass(hass)
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitConnectionError(
        "offline"
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
        },
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.FORM
    assert result_data.get("step_id") == "reauth_confirm"
    assert _as_dict(result_data.get("errors")).get("base") == "cannot_connect"


async def test_reauth_unknown_error_shows_error(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ACCESS_TOKEN: "old_access",
            CONF_REFRESH_TOKEN: "old_refresh",
            CONF_STORE_CREDENTIALS: True,
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
        },
    )
    entry.add_to_hass(hass)
    mock_config_flow_client.async_login.side_effect = Exception("boom")

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
        },
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.FORM
    assert result_data.get("step_id") == "reauth_confirm"
    assert _as_dict(result_data.get("errors")).get("base") == "unknown"


async def test_reauth_mfa_invalid_code_shows_error(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ACCESS_TOKEN: "old_access",
            CONF_REFRESH_TOKEN: "old_refresh",
            CONF_STORE_CREDENTIALS: True,
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
        },
    )
    entry.add_to_hass(hass)
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitMfaRequiredError(
        mfa_token=MOCK_MFA_TOKEN
    )
    mock_config_flow_client.async_verify_mfa.side_effect = nanit_config_flow.NanitAuthError(
        "bad mfa"
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MFA_CODE: "000000"},
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.FORM
    assert result_data.get("step_id") == "reauth_mfa"
    assert _as_dict(result_data.get("errors")).get("base") == "invalid_mfa_code"


async def test_reauth_mfa_connection_error_shows_error(
    hass: HomeAssistant,
    mock_config_flow_client,
) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_ACCESS_TOKEN: "old_access",
            CONF_REFRESH_TOKEN: "old_refresh",
            CONF_STORE_CREDENTIALS: True,
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
        },
    )
    entry.add_to_hass(hass)
    mock_config_flow_client.async_login.side_effect = nanit_config_flow.NanitMfaRequiredError(
        mfa_token=MOCK_MFA_TOKEN
    )
    mock_config_flow_client.async_verify_mfa.side_effect = (
        nanit_config_flow.NanitConnectionError("offline")
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reauth", "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_EMAIL: MOCK_EMAIL,
            CONF_PASSWORD: MOCK_PASSWORD,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MFA_CODE: "000000"},
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.FORM
    assert result_data.get("step_id") == "reauth_mfa"
    assert _as_dict(result_data.get("errors")).get("base") == "cannot_connect"


async def test_options_flow_init_no_cameras_aborts(hass: HomeAssistant) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.runtime_data = SimpleNamespace(
        hub=SimpleNamespace(babies=[])
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result.get("type") is FlowResultType.ABORT
    assert result.get("reason") == "no_cameras"


async def test_options_flow_init_single_camera_goes_to_camera_ip(
    hass: HomeAssistant,
) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.runtime_data = SimpleNamespace(
        hub=SimpleNamespace(babies=[MOCK_BABY_1])
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "camera_ip"


async def test_options_flow_init_multiple_cameras_shows_selector(
    hass: HomeAssistant,
) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, options={})
    entry.runtime_data = SimpleNamespace(
        hub=SimpleNamespace(babies=[MOCK_BABY_1, MOCK_BABY_2])
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "init"


async def test_options_flow_camera_ip_sets_ip(hass: HomeAssistant) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_CAMERA_IPS: {}})
    entry.runtime_data = SimpleNamespace(
        hub=SimpleNamespace(babies=[MOCK_BABY_1])
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CAMERA_IP: "192.168.1.25"},
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.CREATE_ENTRY
    assert result_data["data"][CONF_CAMERA_IPS] == {MOCK_BABY_1.camera_uid: "192.168.1.25"}


async def test_options_flow_multi_camera_select_then_set_ip(
    hass: HomeAssistant,
) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_CAMERA_IPS: {}})
    entry.runtime_data = SimpleNamespace(
        hub=SimpleNamespace(babies=[MOCK_BABY_1, MOCK_BABY_2])
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"camera": MOCK_BABY_2.camera_uid},
    )
    assert result.get("type") is FlowResultType.FORM
    assert result.get("step_id") == "camera_ip"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CAMERA_IP: "192.168.1.26"},
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.CREATE_ENTRY
    assert result_data["data"][CONF_CAMERA_IPS] == {MOCK_BABY_2.camera_uid: "192.168.1.26"}


async def test_options_flow_camera_ip_clears_ip_when_empty(
    hass: HomeAssistant,
) -> None:
    hass = await _resolve_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_CAMERA_IPS: {MOCK_BABY_1.camera_uid: "192.168.1.30"}},
    )
    entry.runtime_data = SimpleNamespace(
        hub=SimpleNamespace(babies=[MOCK_BABY_1])
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CAMERA_IP: ""},
    )

    result_data = _as_dict(result)
    assert result_data.get("type") is FlowResultType.CREATE_ENTRY
    assert result_data["data"][CONF_CAMERA_IPS] == {}
