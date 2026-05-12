"""Coordinator tests for the Yorkshire Water integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pyyorkshirewater import MeterStatus, YorkshireWaterAPIError

from custom_components.yorkshire_water.bridge_auth import (
    BridgeLoginError,
    BridgeUnreachableError,
)
from custom_components.yorkshire_water.const import DOMAIN

from .conftest import SAMPLE_CREDENTIALS


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Yorkshire Water (test)",
        data=dict(SAMPLE_CREDENTIALS),
        unique_id="yw-test",
        options={},
        version=3,
        minor_version=0,
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_entry_with_live_meter(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data.coordinator
    assert coordinator.data is not None
    assert len(coordinator.data.properties) == 1
    assert coordinator.data.properties[0].meter_status is MeterStatus.LIVE
    assert mock_client_live.get_daily_consumption.await_count >= 1


async def test_setup_entry_with_pending_meter_skips_consumption(
    hass: HomeAssistant,
    mock_client_pending: MagicMock,
) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert (
        coordinator.data.properties[0].meter_status is MeterStatus.PENDING_ACTIVATION
    )
    assert mock_client_pending.get_daily_consumption.await_count == 0


async def test_setup_entry_with_no_meter(
    hass: HomeAssistant,
    mock_client_no_meter: MagicMock,
) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.data.properties[0].meter_status is MeterStatus.NO_METER


async def test_bridge_login_failure_does_not_block_setup(
    hass: HomeAssistant,
) -> None:
    """BridgeLoginError on the bootstrap refresh must not abort setup.

    From v0.9.2 setup completes (state LOADED) regardless of whether
    the bootstrap refresh succeeded. The schedule is registered so the
    next configured clock time is the natural retry path; sensors stay
    unavailable until the first successful fetch. Reauth is a noisy
    interruption and most "login failed" causes are transient
    (reCAPTCHA cooldown, YW form glitches), so we never raise from
    setup on a transient login failure.
    """
    entry = _entry(hass)

    with patch(
        "custom_components.yorkshire_water.coordinator.bridge_login",
        new=AsyncMock(side_effect=BridgeLoginError("rejected")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data.coordinator
    assert coordinator.last_update_success is False


async def test_bridge_unreachable_does_not_block_setup(
    hass: HomeAssistant,
) -> None:
    """Bridge unreachable at bootstrap is recorded, not raised.

    Same reasoning as the BridgeLoginError test: setup must complete
    so the daily schedule is registered.
    """
    entry = _entry(hass)

    with patch(
        "custom_components.yorkshire_water.coordinator.bridge_login",
        new=AsyncMock(side_effect=BridgeUnreachableError("not reachable")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data.coordinator
    assert coordinator.last_update_success is False


async def test_api_error_during_refresh_marks_unavailable(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    mock_client_live.get_meter_details = AsyncMock(side_effect=YorkshireWaterAPIError("503"))
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
