"""Sensor tests for the Yorkshire Water integration."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.yorkshire_water.const import DOMAIN

from .conftest import SAMPLE_CREDENTIALS

# Entity-id slug derived from the test fixture address
# "1 Example Street, Sometown, Anywhere, EX1 1EX".
PROPERTY_SLUG = "1_example_street_sometown_anywhere_ex1_1ex"


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


async def test_sensors_when_meter_live(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    yesterday = hass.states.get(f"sensor.{PROPERTY_SLUG}_consumption_yesterday")
    window = hass.states.get(f"sensor.{PROPERTY_SLUG}_consumption_last_8_days")
    meter_ref = hass.states.get(f"sensor.{PROPERTY_SLUG}_meter_reference")
    cumulative = hass.states.get(f"sensor.{PROPERTY_SLUG}_cumulative_consumption")

    # There is no "today" sensor: YW only publish complete daily totals.
    assert hass.states.get(f"sensor.{PROPERTY_SLUG}_consumption_today") is None
    assert yesterday is not None
    assert yesterday.state not in (None, STATE_UNAVAILABLE)
    assert window is not None
    assert meter_ref is not None
    assert meter_ref.state == "WAKE-001"
    # Cumulative sensor should report the sum of all daily points
    # (95 + 110.5 + 78 = 283.5 from the test fixture).
    assert cumulative is not None
    assert float(cumulative.state) == pytest.approx(283.5)
    # Energy Dashboard requires the right device class and state class.
    assert cumulative.attributes["device_class"] == "water"
    assert cumulative.attributes["state_class"] == "total_increasing"


async def test_cumulative_sensor_is_monotonic(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    """If a poll returns less data than the previous one, cumulative does not drop."""
    from pyyorkshirewater import DailyConsumptionPoint

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    cumulative_first = hass.states.get(
        f"sensor.{PROPERTY_SLUG}_cumulative_consumption",
    )
    first_value = float(cumulative_first.state)

    mock_client_live.get_daily_consumption.return_value = [
        DailyConsumptionPoint.from_api(
            {"date": "2026-05-06", "totalConsumptionLitres": 78.0},
        ),
    ]
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    cumulative_after = hass.states.get(
        f"sensor.{PROPERTY_SLUG}_cumulative_consumption",
    )
    # The cumulative should not decrease, even though the window now
    # contains less data.
    assert float(cumulative_after.state) >= first_value


async def test_sensors_unavailable_when_pending(
    hass: HomeAssistant,
    mock_client_pending: MagicMock,
) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    yesterday = hass.states.get(f"sensor.{PROPERTY_SLUG}_consumption_yesterday")
    window = hass.states.get(f"sensor.{PROPERTY_SLUG}_consumption_last_8_days")
    meter_ref = hass.states.get(f"sensor.{PROPERTY_SLUG}_meter_reference")

    assert yesterday is not None
    assert yesterday.state == STATE_UNAVAILABLE
    assert window is not None
    assert window.state == STATE_UNAVAILABLE
    assert meter_ref is not None
    assert meter_ref.state == "WAKE-001"
