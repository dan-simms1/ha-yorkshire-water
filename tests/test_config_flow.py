"""Config flow tests for the Yorkshire Water integration.

Note: from v0.8.3 the config flow no longer performs a full YW login
during user/reauth steps. It does a lightweight bridge-reachability
check (`bridge_healthcheck`) and then trusts the credentials; the real
login happens at the coordinator's first refresh. Tests reflect that:
the only "auth-style" failure surfaced inline is `playwright_unreachable`.
Bad credentials surface later as a setup-error / reauth prompt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.yorkshire_water.bridge_auth import BridgeUnreachableError
from custom_components.yorkshire_water.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PLAYWRIGHT_URL,
    DOMAIN,
)

from .conftest import SAMPLE_CREDENTIALS


async def test_user_flow_happy_path(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    """A reachable bridge URL creates an entry. Login is deferred to setup."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        SAMPLE_CREDENTIALS,
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_EMAIL] == SAMPLE_CREDENTIALS["email"]
    assert result["data"][CONF_PASSWORD] == SAMPLE_CREDENTIALS["password"]
    assert result["data"][CONF_PLAYWRIGHT_URL] == SAMPLE_CREDENTIALS["playwright_url"]


async def test_user_flow_handles_bridge_unreachable(hass: HomeAssistant) -> None:
    """BridgeUnreachableError surfaces as playwright_unreachable."""
    with patch(
        "custom_components.yorkshire_water.config_flow.bridge_healthcheck",
        new=AsyncMock(side_effect=BridgeUnreachableError("not reachable")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            SAMPLE_CREDENTIALS,
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "playwright_unreachable"}


async def test_user_flow_handles_unexpected_error(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.yorkshire_water.config_flow.bridge_healthcheck",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            SAMPLE_CREDENTIALS,
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


@pytest.mark.usefixtures("mock_client_live")
async def test_user_flow_aborts_when_already_configured(hass: HomeAssistant) -> None:
    first = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    await hass.config_entries.flow.async_configure(
        first["flow_id"],
        SAMPLE_CREDENTIALS,
    )
    await hass.async_block_till_done()

    second = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.flow.async_configure(
        second["flow_id"],
        SAMPLE_CREDENTIALS,
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
