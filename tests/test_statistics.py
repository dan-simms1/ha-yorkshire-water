"""Tests for the statistics backfill (v3 ledger + monotonic sum)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pyyorkshirewater import (
    DailyConsumptionPoint,
    MeterStatus,
    YearlyConsumption,
)

from custom_components.yorkshire_water.const import DOMAIN
from custom_components.yorkshire_water.coordinator import PropertyData
from custom_components.yorkshire_water.statistics import (
    async_import_property_statistics,
)

from .conftest import _property

ACCOUNT = "1234567890123456"
_STAT_PATCH = (
    "custom_components.yorkshire_water.statistics.async_add_external_statistics"
)
_STORE_PATCH = "custom_components.yorkshire_water.statistics._ledger_store"


class _MemoryStore:
    """In-memory stand-in for HA's Store."""

    def __init__(self) -> None:
        self.data: Any = None

    async def async_load(self) -> Any:
        return self.data

    async def async_save(self, data: Any) -> None:
        self.data = data

    async def async_remove(self) -> None:
        self.data = None


def _point(day: date, litres: Any = None, cost: Any = None, missing: bool = False):
    payload: dict[str, Any] = {"date": day.isoformat(), "isMissingConsumption": missing}
    if litres is not None:
        payload["totalConsumptionLitres"] = litres
    if cost is not None:
        payload["totalCostIncludingSewerage"] = cost
    return DailyConsumptionPoint.from_api(payload)


def _data(points, yearly=None) -> PropertyData:
    return PropertyData(
        property=_property(),
        meter_status=MeterStatus.LIVE,
        daily_points=points,
        yearly_consumption=yearly,
    )


async def test_daily_sum_is_globally_monotonic(hass: HomeAssistant) -> None:
    """The imported daily statistic carries an absolute, increasing sum."""
    store = _MemoryStore()
    emitted: dict[str, list[dict[str, Any]]] = {}

    def capture(_hass, meta, stats):
        emitted[meta["statistic_id"]] = list(stats)

    d0 = date(2026, 6, 1)
    points = [_point(d0 + timedelta(days=i), 100 + i, 0.5) for i in range(5)]
    with patch(_STORE_PATCH, return_value=store), patch(_STAT_PATCH, side_effect=capture):
        await async_import_property_statistics(hass, "e1", _data(points))

    sums = [row["sum"] for row in emitted[f"{DOMAIN}:daily_consumption_{ACCOUNT}"]]
    assert sums == sorted(sums)  # non-decreasing
    assert sums[-1] == 100 + 101 + 102 + 103 + 104  # absolute total


async def test_revised_daily_value_rewrites_following_sums(hass: HomeAssistant) -> None:
    """A revised past day re-states every subsequent absolute sum."""
    store = _MemoryStore()
    emitted: list[tuple[str, list[dict[str, Any]]]] = []

    def capture(_hass, meta, stats):
        emitted.append((meta["statistic_id"], list(stats)))

    d1 = date(2026, 6, 1)
    with patch(_STORE_PATCH, return_value=store), patch(_STAT_PATCH, side_effect=capture):
        await async_import_property_statistics(
            hass, "e1",
            _data([_point(d1, 10), _point(d1 + timedelta(days=1), 20),
                   _point(d1 + timedelta(days=2), 30)]),
        )
        await async_import_property_statistics(
            hass, "e1", _data([_point(d1 + timedelta(days=1), 15)]),
        )

    latest = [rows for sid, rows in emitted
              if sid == f"{DOMAIN}:daily_consumption_{ACCOUNT}"][-1]
    assert [r["sum"] for r in latest] == [10.0, 25.0, 55.0]


async def test_null_cost_reread_keeps_prior_cost(hass: HomeAssistant) -> None:
    """A later reading with null cost must not wipe a recorded cost."""
    store = _MemoryStore()
    with patch(_STORE_PATCH, return_value=store), patch(_STAT_PATCH):
        d = date(2026, 6, 1)
        await async_import_property_statistics(hass, "e1", _data([_point(d, 10, 0.40)]))
        # Re-read: litres present, cost dropped to null.
        await async_import_property_statistics(hass, "e1", _data([_point(d, 10)]))

    assert store.data[ACCOUNT]["daily"][d.isoformat()] == [10.0, 0.40]


async def test_malformed_month_does_not_block_daily(hass: HomeAssistant) -> None:
    """An out-of-range month is skipped, not fatal, so daily still imports."""
    store = _MemoryStore()
    emitted: list[str] = []
    yearly = YearlyConsumption.from_api({
        "year": 2026,
        "monthlyConsumption": [{"month": "13", "totalConsumptionLitres": 1}],
    })
    with patch(_STORE_PATCH, return_value=store), \
            patch(_STAT_PATCH, side_effect=lambda _h, m, s: emitted.append(m["statistic_id"])):
        await async_import_property_statistics(
            hass, "e1", _data([_point(date(2026, 6, 1), 7)], yearly),
        )

    assert f"{DOMAIN}:daily_consumption_{ACCOUNT}" in emitted
    # No monthly buckets emitted for the bogus month.
    assert f"{DOMAIN}:monthly_consumption_{ACCOUNT}" not in emitted


async def test_corrupt_ledger_does_not_raise(hass: HomeAssistant) -> None:
    """A garbage ledger payload is tolerated, not fatal."""
    store = _MemoryStore()
    store.data = {ACCOUNT: "not-a-dict"}
    with patch(_STORE_PATCH, return_value=store), patch(_STAT_PATCH):
        await async_import_property_statistics(
            hass, "e1", _data([_point(date(2026, 6, 1), 7, 0.3)]),
        )
    assert isinstance(store.data[ACCOUNT], dict)
    assert "daily" in store.data[ACCOUNT]
