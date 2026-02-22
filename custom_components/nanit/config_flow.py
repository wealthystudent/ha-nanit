"""Config flow for Nanit integration."""

from __future__ import annotations

import os
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_EMAIL, CONF_HOST, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import (
    NanitAuthClient,
    NanitAuthError,
    NanitConnectionError,
    NanitMfaRequiredError,
)
from .const import (
    ADDON_HOST_MARKER,
    ADDON_SLUG,
    CONF_BABY_NAME,
    CONF_BABY_UID,
    CONF_CAMERA_IP,
    CONF_CAMERA_UID,
    CONF_MFA_CODE,
    CONF_MFA_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_STORE_CREDENTIALS,
    CONF_TRANSPORT,
    CONF_USE_ADDON,
    DEFAULT_HOST,
    DOMAIN,
    LOGGER,
    TRANSPORT_LOCAL,
    TRANSPORT_LOCAL_CLOUD,
)


class NanitConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nanit."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._email: str = ""
        self._password: str = ""
        self._host: str = DEFAULT_HOST
        self._store_credentials: bool = False
        self._mfa_token: str = ""
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._baby_uid: str = ""
        self._camera_uid: str = ""
        self._baby_name: str = ""
        self._use_addon: bool = False
        self._addon_hostname: str | None = None

    async def _async_get_addon_info(self) -> dict[str, Any] | None:
        """Query Supervisor API for the nanitd add-on info.

        Returns addon info dict if addon is installed and running, None otherwise.
        Resolves the full slug dynamically (third-party add-ons get a hash prefix).
        """
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                # First, find the full slug by listing all add-ons
                resp = await session.get(
                    "http://supervisor/addons",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                if resp.status != 200:
                    return None
                data = await resp.json()
                addons = data.get("data", {}).get("addons", [])
                full_slug = None
                for addon in addons:
                    slug = addon.get("slug", "")
                    if slug == ADDON_SLUG or slug.endswith(f"_{ADDON_SLUG}"):
                        full_slug = slug
                        break

                if not full_slug:
                    LOGGER.debug("Add-on with slug ending in '%s' not found", ADDON_SLUG)
                    return None

                # Now get detailed info using the full slug
                resp = await session.get(
                    f"http://supervisor/addons/{full_slug}/info",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                if resp.status != 200:
                    LOGGER.debug(
                        "Supervisor returned %s for addon %s", resp.status, full_slug
                    )
                    return None
                data = await resp.json()
                return data.get("data")
        except Exception:
            LOGGER.debug("Failed to query Supervisor for addon info", exc_info=True)
            return None

    async def _async_is_addon_running(self) -> bool:
        """Check if the nanitd add-on is installed and running."""
        info = await self._async_get_addon_info()
        if info is None:
            return False
        state = info.get("state")
        hostname = info.get("hostname")
        if state == "started" and hostname:
            self._addon_hostname = hostname
            return True
        return False

    def _is_hassio(self) -> bool:
        """Check if running under HA Supervisor."""
        return "SUPERVISOR_TOKEN" in os.environ

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial user step."""
        # If running under Supervisor, check for the nanitd add-on first
        if self._is_hassio() and user_input is None:
            addon_running = await self._async_is_addon_running()
            if addon_running:
                # Offer the user a choice: use add-on or manual host
                return await self.async_step_addon_confirm()

        return await self.async_step_credentials(user_input)

    async def async_step_addon_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm using the detected nanitd add-on."""
        if user_input is not None:
            use_addon = user_input.get(CONF_USE_ADDON, True)
            if use_addon:
                self._use_addon = True
                self._host = ADDON_HOST_MARKER
            return await self.async_step_credentials()

        return self.async_show_form(
            step_id="addon_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USE_ADDON, default=True): cv.boolean,
                }
            ),
            description_placeholders={"addon_name": "Nanit Daemon"},
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle credential entry (email, password, optionally host)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]
            if not self._use_addon:
                self._host = user_input.get(CONF_HOST, DEFAULT_HOST)
            self._store_credentials = user_input.get(CONF_STORE_CREDENTIALS, False)

            try:
                async with aiohttp.ClientSession() as session:
                    auth_client = NanitAuthClient(session)
                    result = await auth_client.login(self._email, self._password)

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

        # Build schema — hide host field if using add-on
        if self._use_addon:
            schema = vol.Schema(
                {
                    vol.Required(CONF_EMAIL): cv.string,
                    vol.Required(CONF_PASSWORD): cv.string,
                    vol.Optional(CONF_STORE_CREDENTIALS, default=False): cv.boolean,
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(CONF_EMAIL): cv.string,
                    vol.Required(CONF_PASSWORD): cv.string,
                    vol.Optional(CONF_HOST, default=DEFAULT_HOST): cv.string,
                    vol.Optional(CONF_STORE_CREDENTIALS, default=False): cv.boolean,
                }
            )

        return self.async_show_form(
            step_id="credentials",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle MFA code entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            mfa_code = user_input[CONF_MFA_CODE]
            try:
                async with aiohttp.ClientSession() as session:
                    auth_client = NanitAuthClient(session)
                    result = await auth_client.verify_mfa(
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
        """Fetch baby list and continue to transport step."""
        try:
            async with aiohttp.ClientSession() as session:
                auth_client = NanitAuthClient(session)
                babies = await auth_client.get_babies(self._access_token)

            if babies:
                baby = babies[0]
                self._baby_uid = baby.get("uid", "")
                self._camera_uid = baby.get("camera_uid", self._baby_uid)
                self._baby_name = baby.get("name", "Nanit Camera")
            else:
                self._baby_name = "Nanit Camera"

        except Exception:
            LOGGER.exception("Failed to fetch babies, using defaults")
            self._baby_name = "Nanit Camera"

        return await self.async_step_transport()

    async def async_step_transport(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle transport selection."""
        if user_input is not None:
            transport = user_input[CONF_TRANSPORT]
            camera_ip = user_input.get(CONF_CAMERA_IP, "")

            await self.async_set_unique_id(self._camera_uid or self._email)
            self._abort_if_unique_id_configured()

            data: dict[str, Any] = {
                CONF_HOST: self._host,
                CONF_TRANSPORT: transport,
                CONF_ACCESS_TOKEN: self._access_token,
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_BABY_UID: self._baby_uid,
                CONF_CAMERA_UID: self._camera_uid,
                CONF_BABY_NAME: self._baby_name,
                CONF_STORE_CREDENTIALS: self._store_credentials,
                CONF_USE_ADDON: self._use_addon,
            }

            if camera_ip:
                data[CONF_CAMERA_IP] = camera_ip

            if self._use_addon and self._addon_hostname:
                data[CONF_HOST] = ADDON_HOST_MARKER

            if self._store_credentials:
                data[CONF_EMAIL] = self._email
                data[CONF_PASSWORD] = self._password

            return self.async_create_entry(
                title=self._baby_name,
                data=data,
            )

        return self.async_show_form(
            step_id="transport",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRANSPORT, default=TRANSPORT_LOCAL): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=TRANSPORT_LOCAL, label="local"),
                                SelectOptionDict(value=TRANSPORT_LOCAL_CLOUD, label="local_cloud"),
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="transport",
                        )
                    ),
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

            try:
                async with aiohttp.ClientSession() as session:
                    auth_client = NanitAuthClient(session)
                    result = await auth_client.login(email, password)

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
            try:
                async with aiohttp.ClientSession() as session:
                    auth_client = NanitAuthClient(session)
                    result = await auth_client.verify_mfa(
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
        """Handle reconfiguration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            reconfigure_entry = self._get_reconfigure_entry()
            new_data = {**reconfigure_entry.data}
            new_data[CONF_HOST] = user_input.get(CONF_HOST, DEFAULT_HOST)
            new_data[CONF_TRANSPORT] = user_input[CONF_TRANSPORT]
            camera_ip = user_input.get(CONF_CAMERA_IP, "")
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
                        CONF_HOST,
                        default=reconfigure_entry.data.get(CONF_HOST, DEFAULT_HOST),
                    ): cv.string,
                    vol.Required(
                        CONF_TRANSPORT,
                        default=reconfigure_entry.data.get(CONF_TRANSPORT, TRANSPORT_LOCAL),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=TRANSPORT_LOCAL, label="local"),
                                SelectOptionDict(value=TRANSPORT_LOCAL_CLOUD, label="local_cloud"),
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="transport",
                        )
                    ),
                    vol.Optional(
                        CONF_CAMERA_IP,
                        default=reconfigure_entry.data.get(CONF_CAMERA_IP, ""),
                    ): cv.string,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> NanitOptionsFlow:
        """Get the options flow for this handler."""
        return NanitOptionsFlow()


class NanitOptionsFlow(OptionsFlow):
    """Handle Nanit options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={CONF_TRANSPORT: user_input[CONF_TRANSPORT]},
            )

        # Read current transport from options first, then fall back to data
        current_transport = self.config_entry.options.get(
            CONF_TRANSPORT,
            self.config_entry.data.get(CONF_TRANSPORT, TRANSPORT_LOCAL),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TRANSPORT,
                        default=current_transport,
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=TRANSPORT_LOCAL, label="local"),
                                SelectOptionDict(value=TRANSPORT_LOCAL_CLOUD, label="local_cloud"),
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="transport",
                        )
                    ),
                }
            ),
        )
