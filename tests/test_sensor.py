"""Sensor tests for the Yorkshire Water integration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.yorkshire_water.const import DOMAIN

from .conftest import SAMPLE_CREDENTIALS, _property

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

    # v3.0 surface: no daily/yesterday/window/cumulative live sensors -
    # the dated history lives in long-term statistics. The live sensors
    # are current-value + diagnostics only.
    for gone in (
        "consumption_today",
        "consumption_yesterday",
        "cost_yesterday",
        "window_consumption",
        "cumulative_consumption",
        "cumulative_cost",
        "consumption_last_month",
        "cost_last_month_total",
        "average_monthly_consumption",
        "average_monthly_cost",
    ):
        assert hass.states.get(f"sensor.{PROPERTY_SLUG}_{gone}") is None

    meter_ref = hass.states.get(f"sensor.{PROPERTY_SLUG}_meter_reference")
    assert meter_ref is not None
    assert meter_ref.state == "WAKE-001"

    # Latest daily reading diagnostic: freshest fixture day (78.0) with
    # its date + lag exposed as attributes, and no state_class (so it is
    # not recorded into long-term statistics).
    latest = hass.states.get(f"sensor.{PROPERTY_SLUG}_latest_daily_consumption")
    assert latest is not None
    assert float(latest.state) == pytest.approx(78.0)
    assert "reading_date" in latest.attributes
    assert "lag_days" in latest.attributes
    assert latest.attributes.get("state_class") is None

    # Month-to-date total sensor exists (its value depends on the
    # your-usage payload, which this fixture does not populate).
    assert hass.states.get(f"sensor.{PROPERTY_SLUG}_consumption_this_month") is not None

    # Meter status still carries the property address attribute.
    meter_status = hass.states.get(f"sensor.{PROPERTY_SLUG}_meter_status")
    assert meter_status is not None
    assert "Example Street" in meter_status.attributes.get("address", "")


async def test_sensors_unavailable_when_pending(
    hass: HomeAssistant,
    mock_client_pending: MagicMock,
) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    latest = hass.states.get(f"sensor.{PROPERTY_SLUG}_latest_daily_consumption")
    meter_ref = hass.states.get(f"sensor.{PROPERTY_SLUG}_meter_reference")

    assert latest is not None
    assert latest.state == STATE_UNAVAILABLE
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


async def test_latest_daily_skips_trailing_missing_day(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    """latest_daily_consumption reflects the freshest REAL reading, not a
    trailing missing/null placeholder day."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pyyorkshirewater import DailyConsumptionPoint

    today = dt_util.now().date()
    real_day = today - timedelta(days=2)
    mock_client_live.get_daily_consumption.return_value = [
        DailyConsumptionPoint.from_api(
            {"date": real_day.isoformat(), "totalConsumptionLitres": 123.0,
             "totalCostIncludingSewerage": 0.44},
        ),
        DailyConsumptionPoint.from_api(
            {"date": (today - timedelta(days=1)).isoformat(),
             "isMissingConsumption": True},
        ),
    ]

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    latest = hass.states.get(f"sensor.{PROPERTY_SLUG}_latest_daily_consumption")
    assert latest is not None
    assert float(latest.state) == pytest.approx(123.0)
    assert latest.attributes["reading_date"] == real_day.isoformat()


async def test_clear_deprecated_statistics_purges_orphans(
    hass: HomeAssistant,
) -> None:
    """v3.0.0 upgrade clears stats for dropped + state-class-stripped sensors.

    The orphaned series are keyed under both the account slug and the
    old address slug (a v1.x install recorded under the address before
    the privacy migration could rename it), and the cleanup runs once.
    """
    from custom_components.yorkshire_water import (
        _STAT_CLASS_REMOVED_KEYS,
        _STATS_CLEANUP_V3,
        _clear_deprecated_statistics,
    )

    entry = _entry(hass)
    prop = _property()
    coordinator = SimpleNamespace(
        data=SimpleNamespace(properties=[SimpleNamespace(property=prop)]),
    )
    address_slug = slugify(prop.address.formatted())
    recorder = MagicMock()

    with patch(
        "custom_components.yorkshire_water.get_instance", return_value=recorder,
    ):
        _clear_deprecated_statistics(hass, entry, coordinator)

    recorder.async_clear_statistics.assert_called_once()
    cleared = set(recorder.async_clear_statistics.call_args.args[0])
    # A dropped sensor and a state-class-stripped summary, under both the
    # account slug and the legacy address slug.
    assert f"sensor.{PROPERTY_SLUG}_cumulative_consumption" in cleared
    assert f"sensor.{address_slug}_cumulative_consumption" in cleared
    for key in _STAT_CLASS_REMOVED_KEYS:
        assert f"sensor.{PROPERTY_SLUG}_{key}" in cleared
    # The flag is set, so a second pass is a no-op.
    assert entry.data.get(_STATS_CLEANUP_V3) is True
    recorder.reset_mock()
    with patch(
        "custom_components.yorkshire_water.get_instance", return_value=recorder,
    ):
        _clear_deprecated_statistics(hass, entry, coordinator)
    recorder.async_clear_statistics.assert_not_called()


async def test_clear_deprecated_statistics_keeps_flow_rate(
    hass: HomeAssistant,
) -> None:
    """continuous_flow_rate keeps its state_class, so its stats are spared."""
    from custom_components.yorkshire_water import _clear_deprecated_statistics

    entry = _entry(hass)
    coordinator = SimpleNamespace(
        data=SimpleNamespace(properties=[SimpleNamespace(property=_property())]),
    )
    recorder = MagicMock()
    with patch(
        "custom_components.yorkshire_water.get_instance", return_value=recorder,
    ):
        _clear_deprecated_statistics(hass, entry, coordinator)

    cleared = set(recorder.async_clear_statistics.call_args.args[0])
    assert not any("continuous_flow_rate" in sid for sid in cleared)
