"""Config flow for Nanit integration."""

from __future__ import annotations

from typing import Any

import aiohttp
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

from aionanit import NanitClient, NanitAuthError, NanitConnectionError, NanitMfaRequiredError

from .const import (
    CONF_BABY_NAME,
    CONF_BABY_UID,
    CONF_CAMERA_IP,
    CONF_CAMERA_UID,
    CONF_MFA_CODE,
    CONF_MFA_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_STORE_CREDENTIALS,
    DOMAIN,
    LOGGER,
)


class NanitConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nanit."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._email: str = ""
        self._password: str = ""
        self._store_credentials: bool = False
        self._mfa_token: str = ""
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._baby_uid: str = ""
        self._camera_uid: str = ""
        self._baby_name: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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

            session = async_get_clientsession(self.hass)
            client = NanitClient(session)

            try:
                result = await client.async_login(self._email, self._password)
                self._access_token = result["access_token"]
                self._refresh_token = result["refresh_token"]

            except NanitMfaRequiredError as err:
                self._mfa_token = err.mfa_token
                return await self.async_step_mfa()

            except NanitAuthError:
                errors["base"] = "invalid_auth"
            except NanitConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected error during login")
                errors["base"] = "unknown"
            else:
                return await self._async_fetch_babies_and_continue()

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

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle MFA code entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            mfa_code = user_input[CONF_MFA_CODE]
            session = async_get_clientsession(self.hass)
            client = NanitClient(session)

            try:
                result = await client.async_verify_mfa(
                    self._email, self._password, self._mfa_token, mfa_code
                )
                self._access_token = result["access_token"]
                self._refresh_token = result["refresh_token"]

            except NanitAuthError:
                errors["base"] = "invalid_mfa_code"
            except NanitConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected error during MFA verification")
                errors["base"] = "unknown"
            else:
                return await self._async_fetch_babies_and_continue()

        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MFA_CODE): cv.string,
                }
            ),
            errors=errors,
        )

    async def _async_fetch_babies_and_continue(self) -> ConfigFlowResult:
        """Fetch baby list and continue to camera IP step."""
        session = async_get_clientsession(self.hass)
        client = NanitClient(session)
        client.restore_tokens(self._access_token, self._refresh_token)

        try:
            babies = await client.async_get_babies()
            if babies:
                baby = babies[0]
                self._baby_uid = baby.uid
                self._camera_uid = baby.camera_uid
                self._baby_name = baby.name
            else:
                self._baby_name = "Nanit Camera"
        except Exception:
            LOGGER.exception("Failed to fetch babies, using defaults")
            self._baby_name = "Nanit Camera"

        return await self.async_step_camera_ip()

    async def async_step_camera_ip(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle optional camera IP entry for local access."""
        if user_input is not None:
            camera_ip = user_input.get(CONF_CAMERA_IP, "").strip()

            await self.async_set_unique_id(self._camera_uid or self._email)
            self._abort_if_unique_id_configured()

            data: dict[str, Any] = {
                CONF_ACCESS_TOKEN: self._access_token,
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_BABY_UID: self._baby_uid,
                CONF_CAMERA_UID: self._camera_uid,
                CONF_BABY_NAME: self._baby_name,
                CONF_STORE_CREDENTIALS: self._store_credentials,
            }

            if camera_ip:
                data[CONF_CAMERA_IP] = camera_ip

            if self._store_credentials:
                data[CONF_EMAIL] = self._email
                data[CONF_PASSWORD] = self._password

            return self.async_create_entry(
                title=self._baby_name,
                data=data,
            )

        return self.async_show_form(
            step_id="camera_ip",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_CAMERA_IP, default=""): cv.string,
                }
            ),
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
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

            session = async_get_clientsession(self.hass)
            client = NanitClient(session)

            try:
                result = await client.async_login(email, password)
                access_token = result["access_token"]
                refresh_token = result["refresh_token"]

            except NanitMfaRequiredError as err:
                self._email = email
                self._password = password
                self._mfa_token = err.mfa_token
                return await self.async_step_reauth_mfa()

            except NanitAuthError:
                errors["base"] = "invalid_auth"
            except NanitConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"
            else:
                reauth_entry = self._get_reauth_entry()
                new_data = {**reauth_entry.data}
                new_data[CONF_ACCESS_TOKEN] = access_token
                new_data[CONF_REFRESH_TOKEN] = refresh_token
                if reauth_entry.data.get(CONF_STORE_CREDENTIALS):
                    new_data[CONF_EMAIL] = email
                    new_data[CONF_PASSWORD] = password
                return self.async_update_reload_and_abort(
                    reauth_entry, data=new_data
                )

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
        errors: dict[str, str] = {}

        if user_input is not None:
            mfa_code = user_input[CONF_MFA_CODE]
            session = async_get_clientsession(self.hass)
            client = NanitClient(session)

            try:
                result = await client.async_verify_mfa(
                    self._email, self._password, self._mfa_token, mfa_code
                )
                access_token = result["access_token"]
                refresh_token = result["refresh_token"]

            except NanitAuthError:
                errors["base"] = "invalid_mfa_code"
            except NanitConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                LOGGER.exception("Unexpected error during reauth MFA")
                errors["base"] = "unknown"
            else:
                reauth_entry = self._get_reauth_entry()
                new_data = {**reauth_entry.data}
                new_data[CONF_ACCESS_TOKEN] = access_token
                new_data[CONF_REFRESH_TOKEN] = refresh_token
                if reauth_entry.data.get(CONF_STORE_CREDENTIALS):
                    new_data[CONF_EMAIL] = self._email
                    new_data[CONF_PASSWORD] = self._password
                return self.async_update_reload_and_abort(
                    reauth_entry, data=new_data
                )

        return self.async_show_form(
            step_id="reauth_mfa",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MFA_CODE): cv.string,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reconfiguration — update camera IP."""
        if user_input is not None:
            reconfigure_entry = self._get_reconfigure_entry()
            new_data = {**reconfigure_entry.data}
            camera_ip = user_input.get(CONF_CAMERA_IP, "").strip()
            if camera_ip:
                new_data[CONF_CAMERA_IP] = camera_ip
            else:
                new_data.pop(CONF_CAMERA_IP, None)
            return self.async_update_reload_and_abort(
                reconfigure_entry, data=new_data
            )

        reconfigure_entry = self._get_reconfigure_entry()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_CAMERA_IP,
                        default=reconfigure_entry.data.get(CONF_CAMERA_IP, ""),
                    ): cv.string,
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> NanitOptionsFlow:
        """Get the options flow for this handler."""
        return NanitOptionsFlow()


class NanitOptionsFlow(OptionsFlow):
    """Handle Nanit options — configure camera IP for local access."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            camera_ip = user_input.get(CONF_CAMERA_IP, "").strip()
            return self.async_create_entry(
                title="",
                data={CONF_CAMERA_IP: camera_ip} if camera_ip else {},
            )

        current_ip = self.config_entry.options.get(
            CONF_CAMERA_IP,
            self.config_entry.data.get(CONF_CAMERA_IP, ""),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_CAMERA_IP,
                        default=current_ip,
                    ): cv.string,
                }
            ),
        )
