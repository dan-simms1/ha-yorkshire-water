"""Sensor tests for the Yorkshire Water integration."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.yorkshire_water.const import DOMAIN

from .conftest import SAMPLE_CREDENTIALS

# Account-based entity_id slug used from v2.0 (the fixture's
# display_account_reference). Pre-v2.0 the slug was the address.
PROPERTY_SLUG = "1234567890123456"
_LEGACY_ADDRESS_SLUG = "1_example_street_sometown_anywhere_ex1_1ex"


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
    window = hass.states.get(f"sensor.{PROPERTY_SLUG}_window_consumption")
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

    # Meter status carries the property address as an attribute so the
    # dashboard can render a per-property heading without hard-coding it.
    meter_status = hass.states.get(f"sensor.{PROPERTY_SLUG}_meter_status")
    assert meter_status is not None
    assert "Example Street" in meter_status.attributes.get("address", "")


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
    window = hass.states.get(f"sensor.{PROPERTY_SLUG}_window_consumption")
    meter_ref = hass.states.get(f"sensor.{PROPERTY_SLUG}_meter_reference")

    assert yesterday is not None
    assert yesterday.state == STATE_UNAVAILABLE
    assert window is not None
    assert window.state == STATE_UNAVAILABLE
    assert meter_ref is not None
    assert meter_ref.state == "WAKE-001"


async def test_legacy_address_entity_ids_are_migrated(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    """An existing address-derived entity_id is renamed to the account scheme."""
    ent_reg = er.async_get(hass)
    entry = _entry(hass)

    # Pre-seed a v1.x style registry entry: account-based unique_id but
    # an address-derived entity_id (what HA generated when the device
    # was named after the address).
    legacy = ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{PROPERTY_SLUG}_meter_reference",
        suggested_object_id=f"{_LEGACY_ADDRESS_SLUG}_meter_reference",
        config_entry=entry,
    )
    assert legacy.entity_id == f"sensor.{_LEGACY_ADDRESS_SLUG}_meter_reference"

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # The migration renamed it to the account-based entity_id, keyed off
    # the unchanged unique_id.
    migrated = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{PROPERTY_SLUG}_meter_reference",
    )
    assert migrated == f"sensor.{PROPERTY_SLUG}_meter_reference"
    # The old entity_id no longer exists.
    assert (
        hass.states.get(f"sensor.{_LEGACY_ADDRESS_SLUG}_meter_reference") is None
    )
