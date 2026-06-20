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


# Entry-level health entities are keyed on the domain, not the account.
_LAST_UPDATE = "sensor.yorkshire_water_last_update"
_UPDATE_STATUS = "sensor.yorkshire_water_last_update_status"


async def test_health_entities_present_and_healthy(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    """After a successful poll the health diagnostics read 'all good'."""
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # "Last update" (last attempt) is populated with a real timestamp.
    last_update = hass.states.get(_LAST_UPDATE)
    assert last_update is not None
    assert last_update.state not in (STATE_UNAVAILABLE, "unknown")

    # Status is the OK enum value; no error in the attribute.
    status = hass.states.get(_UPDATE_STATUS)
    assert status is not None
    assert status.state == "ok"
    assert status.attributes.get("last_error") is None
    assert status.attributes.get("last_successful_update") is not None


async def test_account_identity_sensors(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    """Customer name and account number are exposed on the account device,
    with contact details as attributes (not their own states)."""
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    name = hass.states.get("sensor.yorkshire_water_customer_name")
    assert name is not None
    assert name.state == "Mr Test User"
    assert name.attributes.get("email") == "test@example.com"
    assert name.attributes.get("phone") == "07700900000"

    number = hass.states.get("sensor.yorkshire_water_account_number")
    assert number is not None
    # PROPERTY_SLUG 1234567890123456 -> bill grouping.
    assert number.state == "1234 5678 9012 345 6"


async def test_account_device_is_hub_with_button(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    """The account device hosts the refresh button and is the parent hub
    of the per-property smart-meter device."""
    from homeassistant.helpers import device_registry as dr

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    account = dev_reg.async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}_account")},
    )
    assert account is not None
    assert account.name == "Yorkshire Water Account"

    # Refresh button is account-level, not per-property.
    btn = ent_reg.async_get("button.yorkshire_water_refresh_now")
    assert btn is not None
    assert btn.device_id == account.id
    assert hass.states.get(f"button.{PROPERTY_SLUG}_refresh_now") is None

    # The per-property meter device hangs off the account device.
    meter = dev_reg.async_get_device(identifiers={(DOMAIN, PROPERTY_SLUG)})
    assert meter is not None
    assert meter.via_device_id == account.id


async def test_real_refresh_failure_flips_status_and_hides_sensors(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    """A genuine async_refresh() failure sets the status enum via the real
    coordinator path, marks normal sensors unavailable, and keeps the
    status sensor available with the error in its attribute."""
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    from pyyorkshirewater import YorkshireWaterAPIError

    coordinator = entry.runtime_data.coordinator
    first_attempt = coordinator.last_attempt_time
    mock_client_live.get_meter_details.side_effect = YorkshireWaterAPIError("boom")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    status = hass.states.get(_UPDATE_STATUS)
    assert status is not None
    assert status.state == "api_error"
    assert "boom" in (status.attributes.get("last_error") or "")

    # last_update advanced to the failed attempt; entity stays available.
    last_update = hass.states.get(_LAST_UPDATE)
    assert last_update is not None
    assert last_update.state != STATE_UNAVAILABLE
    assert coordinator.last_attempt_time != first_attempt

    # A normal coordinator sensor goes unavailable on failure.
    month = hass.states.get(f"sensor.{PROPERTY_SLUG}_consumption_this_month")
    assert month is not None
    assert month.state == STATE_UNAVAILABLE


async def test_repeated_failure_updates_status(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    """A second consecutive failure still updates the status, even though
    HA suppresses its own listener notification on repeats."""
    from pyyorkshirewater import YorkshireWaterAPIError

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator

    mock_client_live.get_meter_details.side_effect = YorkshireWaterAPIError("first")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert "first" in (hass.states.get(_UPDATE_STATUS).attributes["last_error"])

    mock_client_live.get_meter_details.side_effect = YorkshireWaterAPIError("second")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    status = hass.states.get(_UPDATE_STATUS)
    assert status.state == "api_error"
    assert "second" in (status.attributes["last_error"])


async def test_classify_maps_exceptions_to_status() -> None:
    """The status classifier maps known causes to stable enum values."""
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from pyyorkshirewater import CookieSessionExpiredError

    from custom_components.yorkshire_water.bridge_auth import (
        BridgeLoginError,
        BridgeUnreachableError,
    )
    from custom_components.yorkshire_water.coordinator import (
        YorkshireWaterCoordinator,
    )

    classify = YorkshireWaterCoordinator._classify
    assert classify(UpdateFailed("api boom")) == "api_error"
    wrapped_bridge = UpdateFailed("x")
    wrapped_bridge.__cause__ = BridgeUnreachableError("down")
    assert classify(wrapped_bridge) == "bridge_unreachable"
    wrapped_login = UpdateFailed("x")
    wrapped_login.__cause__ = BridgeLoginError("nope")
    assert classify(wrapped_login) == "login_failed"
    wrapped_cookie = UpdateFailed("x")
    wrapped_cookie.__cause__ = CookieSessionExpiredError("expired")
    assert classify(wrapped_cookie) == "login_failed"
    assert classify(RuntimeError("?")) == "unknown_error"


async def test_latest_daily_reading_date_uses_real_point(
    hass: HomeAssistant,
    mock_client_live: MagicMock,
) -> None:
    """'Latest daily reading date' reflects the newest real daily point
    (a date), not YW's forward-running latest_data_date marker."""
    from homeassistant.util import dt as dt_util

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    reading_date = hass.states.get(f"sensor.{PROPERTY_SLUG}_last_reading_time")
    assert reading_date is not None
    # The fixture's freshest real point is "today"; DATE sensors render
    # as an ISO date string.
    assert reading_date.state == dt_util.now().date().isoformat()


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
