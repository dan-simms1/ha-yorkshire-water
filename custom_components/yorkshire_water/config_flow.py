"""Config flow for the Yorkshire Water integration.

The user provides their Yorkshire Water email, password, and the URL
of a Playwright server (the companion `Playwright Stealth Browser`
add-on by default). The flow validates the credentials by running a
full Playwright-driven login. If that succeeds, the entry is created
with the credentials stored. The coordinator then runs the same login
on every refresh.

Cookie-paste auth was removed in v0.4. YW's hard 30-minute server
session cap means cookie paste cannot keep the integration alive
between manual interventions.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers import selector
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.util import slugify

from .bridge_auth import (
    BridgeUnreachableError,
    bridge_healthcheck,
)
from .const import (
    BROWSER_ENGINE_NODRIVER,
    BROWSER_ENGINES,
    CONF_BROWSER_ENGINE,
    CONF_EMAIL,
    CONF_HEARTBEAT_MINUTES,
    CONF_NODRIVER_URL,
    CONF_PASSWORD,
    CONF_PLAYWRIGHT_URL,
    CONF_REFRESH_TIME,
    CONF_REFRESHES_PER_DAY,
    DEFAULT_BROWSER_ENGINE,
    DEFAULT_HEARTBEAT_MINUTES,
    DEFAULT_NODRIVER_URL,
    DEFAULT_PLAYWRIGHT_URL,
    DEFAULT_REFRESH_TIME,
    DEFAULT_REFRESHES_PER_DAY,
    DOMAIN,
    LOGGER,
    MAX_HEARTBEAT_MINUTES,
    MAX_REFRESHES_PER_DAY,
    MIN_HEARTBEAT_MINUTES,
    MIN_REFRESHES_PER_DAY,
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL),
        ),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD),
        ),
        vol.Required(
            CONF_PLAYWRIGHT_URL,
            default=DEFAULT_PLAYWRIGHT_URL,
        ): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL),
        ),
        vol.Required(
            CONF_NODRIVER_URL,
            default=DEFAULT_NODRIVER_URL,
        ): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL),
        ),
    },
)


def _engine_url(data: Mapping[str, Any], engine: str) -> str:
    """Return the URL of whichever browser-engine addon is currently active."""
    if engine == BROWSER_ENGINE_NODRIVER:
        return data.get(CONF_NODRIVER_URL, DEFAULT_NODRIVER_URL)
    return data.get(CONF_PLAYWRIGHT_URL, DEFAULT_PLAYWRIGHT_URL)


class YorkshireWaterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user-driven config flow."""

    VERSION = 4
    MINOR_VERSION = 0

    def __init__(self) -> None:
        """Initialise reauth state."""
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial user step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Health-check whichever engine is the current default.
            # The user can switch engines later via the options flow;
            # we only need ONE addon reachable to create the entry.
            initial_engine = DEFAULT_BROWSER_ENGINE
            initial_url = _engine_url(user_input, initial_engine)
            try:
                await bridge_healthcheck(
                    initial_url,
                    http_client=get_async_client(self.hass),
                )
            except BridgeUnreachableError as err:
                LOGGER.debug("Bridge unreachable: %s", err)
                errors["base"] = "playwright_unreachable"
            except Exception:
                LOGGER.exception("Unexpected error during config flow validation")
                errors["base"] = "unknown"
            else:
                digest = hashlib.sha256(
                    user_input[CONF_EMAIL].lower().strip().encode("utf-8"),
                ).hexdigest()
                await self.async_set_unique_id(slugify(f"yw-{digest[:16]}"))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Yorkshire Water",
                    data={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_PLAYWRIGHT_URL: user_input[CONF_PLAYWRIGHT_URL],
                        CONF_NODRIVER_URL: user_input[CONF_NODRIVER_URL],
                    },
                    options={
                        CONF_BROWSER_ENGINE: initial_engine,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> ConfigFlowResult:
        """Start a reauth flow when stored credentials stop working."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"],
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Re-collect credentials and validate."""
        errors: dict[str, str] = {}
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_no_entry")

        if user_input is not None:
            current_engine = (
                self._reauth_entry.options.get(
                    CONF_BROWSER_ENGINE, DEFAULT_BROWSER_ENGINE,
                )
            )
            try:
                await bridge_healthcheck(
                    _engine_url(user_input, current_engine),
                    http_client=get_async_client(self.hass),
                )
            except BridgeUnreachableError:
                errors["base"] = "playwright_unreachable"
            except Exception:
                LOGGER.exception("Unexpected error during reauth validation")
                errors["base"] = "unknown"
            else:
                expected_digest = hashlib.sha256(
                    user_input[CONF_EMAIL].lower().strip().encode("utf-8"),
                ).hexdigest()
                expected_unique_id = slugify(f"yw-{expected_digest[:16]}")
                update_kwargs: dict[str, Any] = {
                    "data": {
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_PLAYWRIGHT_URL: user_input[CONF_PLAYWRIGHT_URL],
                        CONF_NODRIVER_URL: user_input[CONF_NODRIVER_URL],
                    },
                }
                if self._reauth_entry.unique_id is None:
                    update_kwargs["unique_id"] = expected_unique_id
                elif self._reauth_entry.unique_id != expected_unique_id:
                    return self.async_abort(reason="reauth_account_mismatch")
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    **update_kwargs,
                )
                await self.hass.config_entries.async_reload(
                    self._reauth_entry.entry_id,
                )
                return self.async_abort(reason="reauth_successful")

        suggestions = {
            CONF_EMAIL: self._reauth_entry.data.get(CONF_EMAIL, ""),
            CONF_PLAYWRIGHT_URL: self._reauth_entry.data.get(
                CONF_PLAYWRIGHT_URL, DEFAULT_PLAYWRIGHT_URL,
            ),
            CONF_NODRIVER_URL: self._reauth_entry.data.get(
                CONF_NODRIVER_URL, DEFAULT_NODRIVER_URL,
            ),
        }
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, suggestions),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Return the options flow handler."""
        return YorkshireWaterOptionsFlow()


class YorkshireWaterOptionsFlow(OptionsFlow):
    """Lets the user pick a clock-time refresh schedule.

    `refresh_time` is the local time of the first refresh each day.
    `refreshes_per_day` (1, 2, 3 or 4) divides the day evenly from
    that time forward, so picking 06:00 + 4/day fires at 06:00,
    12:00, 18:00 and 00:00.
    """

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show or accept the options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # URLs are stored in entry.data at create time but readable
        # from options too. Reading options-first means the user can
        # change them here without going through reauth; if absent
        # from options we fall back to data and finally to defaults.
        entry = self.config_entry
        current_engine = entry.options.get(
            CONF_BROWSER_ENGINE, DEFAULT_BROWSER_ENGINE,
        )
        current_pw_url = entry.options.get(
            CONF_PLAYWRIGHT_URL,
            entry.data.get(CONF_PLAYWRIGHT_URL, DEFAULT_PLAYWRIGHT_URL),
        )
        current_nd_url = entry.options.get(
            CONF_NODRIVER_URL,
            entry.data.get(CONF_NODRIVER_URL, DEFAULT_NODRIVER_URL),
        )
        current_time = entry.options.get(
            CONF_REFRESH_TIME, DEFAULT_REFRESH_TIME,
        )
        current_per_day = entry.options.get(
            CONF_REFRESHES_PER_DAY, DEFAULT_REFRESHES_PER_DAY,
        )
        current_heartbeat = entry.options.get(
            CONF_HEARTBEAT_MINUTES, DEFAULT_HEARTBEAT_MINUTES,
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_BROWSER_ENGINE,
                    default=current_engine,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(BROWSER_ENGINES),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_BROWSER_ENGINE,
                    ),
                ),
                vol.Required(
                    CONF_PLAYWRIGHT_URL,
                    default=current_pw_url,
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.URL),
                ),
                vol.Required(
                    CONF_NODRIVER_URL,
                    default=current_nd_url,
                ): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.URL),
                ),
                vol.Required(
                    CONF_REFRESH_TIME,
                    default=current_time,
                ): selector.TimeSelector(),
                vol.Required(
                    CONF_REFRESHES_PER_DAY,
                    default=current_per_day,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            str(n) for n in range(
                                MIN_REFRESHES_PER_DAY,
                                MAX_REFRESHES_PER_DAY + 1,
                            )
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_REFRESHES_PER_DAY,
                    ),
                ),
                vol.Required(
                    CONF_HEARTBEAT_MINUTES,
                    default=current_heartbeat,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_HEARTBEAT_MINUTES,
                        max=MAX_HEARTBEAT_MINUTES,
                        step=1,
                        unit_of_measurement="min",
                        mode=selector.NumberSelectorMode.BOX,
                    ),
                ),
            },
        )
        return self.async_show_form(step_id="init", data_schema=schema)
