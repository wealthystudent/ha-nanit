"""Config flow for Nanit integration."""

from __future__ import annotations

import ipaddress
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from aionanit import NanitAuthError, NanitClient, NanitConnectionError, NanitMfaRequiredError

from .const import (
    CONF_CAMERA_IP,
    CONF_CAMERA_IPS,
    CONF_MFA_CODE,
    CONF_REFRESH_TOKEN,
    CONF_STORE_CREDENTIALS,
    DOMAIN,
    LOGGER,
)
from .sanitize import sanitize_name


class NanitConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nanit.

    v2: One config entry per Nanit account. All babies/cameras on the
    account are auto-discovered during setup. Camera IPs are configured
    via the options flow.
    """

    VERSION = 2

    def __init__(self) -> None:
        """Initialize."""
        self._email: str = ""
        self._password: str = ""
        self._store_credentials: bool = False
        self._mfa_token: str = ""
        self._access_token: str = ""
        self._refresh_token: str = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial user step — enter credentials."""
        return await self.async_step_credentials(user_input)

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle credential entry (email, password)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]
            self._store_credentials = user_input.get(CONF_STORE_CREDENTIALS, False)
            result = await self._async_attempt_login(
                email=self._email,
                password=self._password,
                unknown_error_log="Unexpected error during login",
                errors=errors,
                on_mfa_step=self.async_step_mfa,
                on_success=self._async_finish_login,
            )
            if result is not None:
                return result

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): cv.string,
                    vol.Required(CONF_PASSWORD): cv.string,
                    vol.Optional(CONF_STORE_CREDENTIALS, default=False): cv.boolean,
                }
            ),
            errors=errors,
        )

    async def async_step_mfa(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle MFA code entry."""
        return await self._async_handle_mfa_step(
            user_input=user_input,
            step_id="mfa",
            unknown_error_log="Unexpected error during MFA verification",
            on_success=self._async_finish_login,
        )

    async def _async_attempt_login(
        self,
        *,
        email: str,
        password: str,
        unknown_error_log: str,
        errors: dict[str, str],
        on_mfa_step: Any,
        on_success: Any,
    ) -> ConfigFlowResult | None:
        """Attempt login and normalize expected errors."""
        session = async_get_clientsession(self.hass)
        client = NanitClient(session)

        try:
            result = await client.async_login(email, password)
        except NanitMfaRequiredError as err:
            self._email = email
            self._password = password
            self._mfa_token = err.mfa_token
            step_result: ConfigFlowResult = await on_mfa_step()
            return step_result
        except NanitAuthError:
            errors["base"] = "invalid_auth"
        except NanitConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:
            LOGGER.exception(unknown_error_log)
            errors["base"] = "unknown"
        else:
            success_result: ConfigFlowResult = await on_success(
                result["access_token"], result["refresh_token"]
            )
            return success_result

        return None

    async def _async_handle_mfa_step(
        self,
        *,
        user_input: dict[str, Any] | None,
        step_id: str,
        unknown_error_log: str,
        on_success: Any,
    ) -> ConfigFlowResult:
        """Handle MFA verification and shared form/error behavior."""
        errors: dict[str, str] = {}

        if user_input is not None:
            mfa_code = user_input[CONF_MFA_CODE]
            session = async_get_clientsession(self.hass)
            client = NanitClient(session)

            try:
                result = await client.async_verify_mfa(
                    self._email, self._password, self._mfa_token, mfa_code
                )
            except NanitAuthError:
                errors["base"] = "invalid_mfa_code"
            except NanitConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception(unknown_error_log)
                errors["base"] = "unknown"
            else:
                mfa_success: ConfigFlowResult = await on_success(
                    result["access_token"], result["refresh_token"]
                )
                return mfa_success

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MFA_CODE): cv.string,
                }
            ),
            errors=errors,
        )

    async def _async_finish_login(self, access_token: str, refresh_token: str) -> ConfigFlowResult:
        """Persist tokens and create the account entry."""
        self._access_token = access_token
        self._refresh_token = refresh_token
        return await self._async_create_account_entry()

    async def _async_create_account_entry(self) -> ConfigFlowResult:
        """Create a config entry for this Nanit account.

        One entry per account — unique_id is the email address.
        All cameras on the account are auto-discovered during setup.
        """
        await self.async_set_unique_id(self._email)
        self._abort_if_unique_id_configured()

        # Determine a friendly title (try to fetch baby names)
        title = "Nanit"
        try:
            session = async_get_clientsession(self.hass)
            client = NanitClient(session)
            client.restore_tokens(self._access_token, self._refresh_token)
            babies = await client.async_get_babies()
            if len(babies) == 1:
                title = babies[0].name
            elif len(babies) > 1:
                title = f"Nanit ({len(babies)} cameras)"
        except Exception:
            LOGGER.debug("Failed to fetch babies for entry title", exc_info=True)

        data: dict[str, Any] = {
            CONF_ACCESS_TOKEN: self._access_token,
            CONF_REFRESH_TOKEN: self._refresh_token,
            CONF_STORE_CREDENTIALS: self._store_credentials,
            CONF_EMAIL: self._email,
        }

        if self._store_credentials:
            data[CONF_PASSWORD] = self._password

        return self.async_create_entry(title=title, data=data)

    # ------------------------------------------------------------------
    # Reauth flow
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauth trigger."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            result = await self._async_attempt_login(
                email=email,
                password=password,
                unknown_error_log="Unexpected error during reauth",
                errors=errors,
                on_mfa_step=self.async_step_reauth_mfa,
                on_success=lambda access_token, refresh_token: self._async_finish_reauth(
                    access_token=access_token,
                    refresh_token=refresh_token,
                    email=email,
                    password=password,
                ),
            )
            if result is not None:
                return result

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): cv.string,
                    vol.Required(CONF_PASSWORD): cv.string,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle MFA during reauth."""
        return await self._async_handle_mfa_step(
            user_input=user_input,
            step_id="reauth_mfa",
            unknown_error_log="Unexpected error during reauth MFA",
            on_success=self._async_finish_reauth,
        )

    async def _async_finish_reauth(
        self,
        access_token: str,
        refresh_token: str,
        email: str | None = None,
        password: str | None = None,
    ) -> ConfigFlowResult:
        """Update the reauth entry with fresh credentials/tokens."""
        reauth_entry = self._get_reauth_entry()
        provided_email = email or self._email

        # Prevent credential swap: the reauth email must match the original.
        if provided_email.lower() != reauth_entry.data[CONF_EMAIL].lower():
            return self.async_abort(reason="reauth_email_mismatch")

        new_data = {**reauth_entry.data}
        new_data[CONF_ACCESS_TOKEN] = access_token
        new_data[CONF_REFRESH_TOKEN] = refresh_token
        if reauth_entry.data.get(CONF_STORE_CREDENTIALS):
            new_data[CONF_PASSWORD] = password or self._password
        return self.async_update_reload_and_abort(reauth_entry, data=new_data)

    # ------------------------------------------------------------------
    # Options flow
    # ------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> NanitOptionsFlow:
        """Get the options flow for this handler."""
        return NanitOptionsFlow()


class NanitOptionsFlow(OptionsFlow):
    """Handle Nanit options — configure camera IPs for local access.

    Two-step flow:
    1. Select which camera to configure (if multiple exist)
    2. Enter or clear the camera IP for local connectivity
    """

    def __init__(self) -> None:
        """Initialize."""
        self._selected_camera_uid: str = ""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select which camera to configure."""
        hub = self.config_entry.runtime_data.hub
        babies = hub.babies

        if not babies:
            return self.async_abort(reason="no_cameras")

        # Single camera — skip selection, go straight to IP config
        if len(babies) == 1:
            self._selected_camera_uid = babies[0].camera_uid
            return await self.async_step_camera_ip(user_input)

        if user_input is not None:
            self._selected_camera_uid = user_input["camera"]
            return await self.async_step_camera_ip()

        camera_options = {baby.camera_uid: sanitize_name(baby.name) for baby in babies}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("camera"): vol.In(camera_options),
                }
            ),
        )

    async def async_step_camera_ip(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure IP for the selected camera."""
        errors: dict[str, str] = {}

        if user_input is not None:
            camera_ip = user_input.get(CONF_CAMERA_IP, "").strip()

            if camera_ip:
                try:
                    ipaddress.ip_address(camera_ip)
                except ValueError:
                    errors[CONF_CAMERA_IP] = "invalid_ip"

            if not errors:
                current_ips = dict(self.config_entry.options.get(CONF_CAMERA_IPS, {}))
                if camera_ip:
                    current_ips[self._selected_camera_uid] = camera_ip
                else:
                    current_ips.pop(self._selected_camera_uid, None)

                return self.async_create_entry(
                    title="",
                    data={CONF_CAMERA_IPS: current_ips},
                )

        current_ip = self.config_entry.options.get(CONF_CAMERA_IPS, {}).get(
            self._selected_camera_uid, ""
        )

        # Resolve camera name for the description placeholder
        camera_name = self._selected_camera_uid
        for baby in self.config_entry.runtime_data.hub.babies:
            if baby.camera_uid == self._selected_camera_uid:
                camera_name = sanitize_name(baby.name)
                break

        return self.async_show_form(
            step_id="camera_ip",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_CAMERA_IP,
                        default=current_ip,
                    ): cv.string,
                }
            ),
            description_placeholders={"camera_name": camera_name},
            errors=errors,
        )
