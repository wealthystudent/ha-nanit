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
    CONF_SPEAKER_IP,
    CONF_SPEAKER_IPS,
    CONF_STORE_CREDENTIALS,
    DOMAIN,
    LOGGER,
)
from .sanitize import display_name


class NanitConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nanit.

    v2: One config entry per Nanit account. All babies/cameras on the
    account are auto-discovered during setup. Camera IPs are configured
    via the options flow.
    """

    VERSION = 2
    # 2.2 drops password persistence (the stored password was never read).
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize."""
        self._email: str = ""
        self._password: str = ""
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
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]
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
        except NanitConnectionError as err:
            LOGGER.debug("Login connection error: %s", err)
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
            except NanitMfaRequiredError as err:
                # The server re-challenged with a fresh mfa_token (the held
                # one went stale). Adopt it so the user's retry verifies
                # against the new token instead of dead-ending forever.
                # Must precede NanitAuthError: it is a subclass of it.
                self._mfa_token = err.mfa_token
                errors["base"] = "invalid_mfa_code"
            except NanitAuthError:
                errors["base"] = "invalid_mfa_code"
            except NanitConnectionError as err:
                LOGGER.debug("MFA verification connection error: %s", err)
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
        # Nanit treats emails case-insensitively, so the unique_id must
        # too, or re-adding the account with different casing creates a
        # duplicate entry with two hubs fighting over the same devices.
        normalized_email = self._email.lower()
        await self.async_set_unique_id(normalized_email)
        self._abort_if_unique_id_configured()
        # Entries created before normalization may carry a mixed-case
        # unique_id, and a migrated v1 entry may still carry a camera uid
        # as its unique_id — match the stored email too so neither can be
        # added a second time.
        for entry in self._async_current_entries(include_ignore=False):
            entry_email = (entry.data.get(CONF_EMAIL) or "").strip().lower()
            if entry_email == normalized_email or (
                entry.unique_id and entry.unique_id.lower() == normalized_email
            ):
                return self.async_abort(reason="already_configured")

        # Determine a friendly title (try to fetch baby names)
        title = "Nanit"
        try:
            session = async_get_clientsession(self.hass)
            client = NanitClient(session)
            client.restore_tokens(self._access_token, self._refresh_token)
            babies = await client.async_get_babies()
            if len(babies) == 1:
                title = display_name(babies[0].name, babies[0].uid)
            elif len(babies) > 1:
                title = f"Nanit ({len(babies)} cameras)"
        except Exception:
            LOGGER.debug("Failed to fetch babies for entry title", exc_info=True)

        data: dict[str, Any] = {
            CONF_ACCESS_TOKEN: self._access_token,
            CONF_REFRESH_TOKEN: self._refresh_token,
            CONF_EMAIL: normalized_email,
        }

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
            email = user_input[CONF_EMAIL].strip()
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
    ) -> ConfigFlowResult:
        """Update the reauth entry with fresh credentials/tokens."""
        reauth_entry = self._get_reauth_entry()
        provided_email = (email or self._email).strip()

        # Prevent credential swap: the reauth email must match the original.
        # An entry with no stored email (possible for a migrated v1 entry
        # that never stored one) has nothing to compare against — accept
        # and adopt the provided email below instead of crashing.
        stored_email = reauth_entry.data.get(CONF_EMAIL, "")
        if stored_email and provided_email.lower() != stored_email.strip().lower():
            return self.async_abort(reason="reauth_email_mismatch")

        new_data = {**reauth_entry.data}
        new_data[CONF_ACCESS_TOKEN] = access_token
        new_data[CONF_REFRESH_TOKEN] = refresh_token
        if not stored_email:
            new_data[CONF_EMAIL] = provided_email
        # Scrub any password persisted by older versions. It was never read
        # by anything (reauth always prompts), so it was pure liability.
        new_data.pop(CONF_PASSWORD, None)
        new_data.pop(CONF_STORE_CREDENTIALS, None)
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
    """Handle Nanit options — configure device IPs for local access.

    Two-step flow:
    1. Select which baby's devices to configure (if multiple exist)
    2. Enter or clear the camera / speaker IP for local connectivity

    Selection is by baby (not camera) so speaker-only babies are
    configurable too. Only the fields for devices the baby actually has
    are shown, and speaker IPs are stored keyed by speaker_uid.
    """

    def __init__(self) -> None:
        """Initialize."""
        self._selected_baby_uid: str = ""

    def _selected_baby(self) -> Any:
        """Return the Baby row for the current selection, or None."""
        for baby in self.config_entry.runtime_data.hub.babies:
            if baby.uid == self._selected_baby_uid:
                return baby
        return None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select which baby's devices to configure."""
        hub = self.config_entry.runtime_data.hub
        babies = hub.babies

        if not babies:
            return self.async_abort(reason="no_cameras")

        # Single baby — skip selection, go straight to IP config
        if len(babies) == 1:
            self._selected_baby_uid = babies[0].uid
            return await self.async_step_camera_ip(user_input)

        if user_input is not None:
            self._selected_baby_uid = user_input["device"]
            return await self.async_step_camera_ip()

        device_options = {baby.uid: display_name(baby.name, baby.uid) for baby in babies}

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): vol.In(device_options),
                }
            ),
        )

    async def async_step_camera_ip(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure IPs for the selected baby's devices."""
        errors: dict[str, str] = {}

        hub = self.config_entry.runtime_data.hub
        baby = self._selected_baby()
        if baby is None:
            return self.async_abort(reason="no_cameras")
        camera_uid: str | None = baby.camera_uid or None
        speaker_uid: str | None = hub.speaker_uid_map.get(baby.uid)

        if user_input is not None:
            camera_ip = user_input.get(CONF_CAMERA_IP, "").strip()
            speaker_ip = user_input.get(CONF_SPEAKER_IP, "").strip()

            if camera_ip:
                try:
                    ipaddress.ip_address(camera_ip)
                except ValueError:
                    errors[CONF_CAMERA_IP] = "invalid_ip"

            if speaker_ip:
                try:
                    ipaddress.ip_address(speaker_ip)
                except ValueError:
                    errors[CONF_SPEAKER_IP] = "invalid_ip"

            if not errors:
                # Merge with existing camera IPs
                current_ips = dict(self.config_entry.options.get(CONF_CAMERA_IPS, {}))
                if camera_uid:
                    if camera_ip:
                        current_ips[camera_uid] = camera_ip
                    else:
                        current_ips.pop(camera_uid, None)

                # Merge with existing speaker IPs, keyed by speaker_uid.
                # When the speaker is resolvable this session, also drop the
                # legacy camera_uid-keyed entry so a cleared IP can't be
                # resurrected by the hub's legacy-read fallback. When it
                # isn't (no speaker field was shown), leave the legacy
                # entry alone — the hub may still be using it.
                current_speaker_ips = dict(self.config_entry.options.get(CONF_SPEAKER_IPS, {}))
                if speaker_uid:
                    if camera_uid:
                        current_speaker_ips.pop(camera_uid, None)
                    if speaker_ip:
                        current_speaker_ips[speaker_uid] = speaker_ip
                    else:
                        current_speaker_ips.pop(speaker_uid, None)

                # Merge over the existing options rather than replacing
                # them wholesale, so any option added elsewhere in the
                # future survives an IP edit.
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_CAMERA_IPS: current_ips,
                        CONF_SPEAKER_IPS: current_speaker_ips,
                    },
                )

        current_ip = self.config_entry.options.get(CONF_CAMERA_IPS, {}).get(camera_uid or "", "")
        stored_speaker_ips = self.config_entry.options.get(CONF_SPEAKER_IPS, {})
        current_speaker_ip = stored_speaker_ips.get(speaker_uid or "") or stored_speaker_ips.get(
            camera_uid or "", ""
        )

        schema_fields: dict[Any, Any] = {}
        if camera_uid:
            schema_fields[
                vol.Optional(CONF_CAMERA_IP, description={"suggested_value": current_ip})
            ] = cv.string
        if speaker_uid:
            schema_fields[
                vol.Optional(CONF_SPEAKER_IP, description={"suggested_value": current_speaker_ip})
            ] = cv.string

        return self.async_show_form(
            step_id="camera_ip",
            data_schema=vol.Schema(schema_fields),
            description_placeholders={"camera_name": display_name(baby.name, baby.uid)},
            errors=errors,
        )
