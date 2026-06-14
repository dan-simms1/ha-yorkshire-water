"""Backfill Yorkshire Water monthly totals into HA long-term statistics.

A native `statistics-graph` card can only chart periods the recorder
already has data for. The live monthly sensors (`consumption_this_month`
etc.) only begin recording when the integration is installed, so months
before that - including the meter's pre-install history that Yorkshire
Water still expose via the `yearly-consumption` endpoint - never appear
on a monthly chart.

This module injects those monthly totals as dedicated EXTERNAL
statistics, one statistic per property, so the chart shows real monthly
bars from the very first poll. We deliberately do NOT backfill the live
sensors' own recorder rows: that would make the recorder claim the
entities held values before they existed, and couple historical data to
entity ids that are derived from the (mutable) property address.

Statistic ids are keyed on the stable `display_account_reference`:
    yorkshire_water:monthly_consumption_<display_account_reference>
    yorkshire_water:monthly_cost_<display_account_reference>

Each calendar month is one bucket. `state` carries that month's total
(what the chart plots with `stat_types: [state]`); `sum` carries a
running cumulative purely to satisfy `has_sum` metadata. Re-importing
the same (statistic_id, start) on every poll is idempotent - the
recorder updates the existing row - so revised YW estimates self-correct.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfVolume
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER

# `mean_type` replaced `has_mean` in HA 2025.10. Support both so the
# integration keeps working on its declared minimum HA version while
# also satisfying newer cores that expect the enum.
try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_NONE: Any | None = StatisticMeanType.NONE
except ImportError:  # HA < 2025.10
    _MEAN_NONE = None

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import PropertyData

_COST_UNIT = "GBP"


def _month_start(year: int, month: int) -> Any:
    """Return the UTC datetime for midnight local on the 1st of the month."""
    return dt_util.as_utc(dt_util.start_of_local_day(date(year, month, 1)))


def _metadata(statistic_id: str, name: str, unit: str) -> dict[str, Any]:
    """Build StatisticMetaData for one external statistic.

    Typed as a plain dict so the optional `mean_type` key can be
    omitted on older HA cores without tripping the TypedDict.

    `unit_class` is set to None deliberately: it is present (so newer
    cores do not warn about its absence) but None skips the recorder's
    unit-conversion validation, which would otherwise reject "GBP" and
    is unnecessary for a standalone monthly bar chart.
    """
    meta: dict[str, Any] = {
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "name": name,
        "unit_of_measurement": unit,
        "unit_class": None,
        "has_mean": False,
        "has_sum": True,
    }
    if _MEAN_NONE is not None:
        meta["mean_type"] = _MEAN_NONE
    return meta


def async_import_monthly_statistics(
    hass: HomeAssistant,
    property_data: PropertyData,
) -> None:
    """Import one property's monthly consumption + cost statistics.

    Safe to call on every coordinator refresh. Does nothing when the
    property has no yearly-consumption summary yet. Never raises for an
    empty or partial month set; the caller wraps this anyway.
    """
    yearly = property_data.yearly_consumption
    if yearly is None or not yearly.year or not yearly.monthly_consumption:
        return

    ref = property_data.property.display_account_reference
    if not ref:
        return

    consumption_id = f"{DOMAIN}:monthly_consumption_{ref}"
    cost_id = f"{DOMAIN}:monthly_cost_{ref}"

    months = sorted(
        (
            period
            for period in yearly.monthly_consumption
            if period.month and period.month.isdigit() and 1 <= int(period.month) <= 12
        ),
        key=lambda period: int(period.month),
    )

    consumption_stats: list[dict[str, Any]] = []
    cost_stats: list[dict[str, Any]] = []
    running_litres = 0.0
    running_cost = 0.0

    for period in months:
        start = _month_start(yearly.year, int(period.month))
        if period.total_consumption_litres is not None:
            litres = float(period.total_consumption_litres)
            running_litres += litres
            consumption_stats.append(
                {"start": start, "state": litres, "sum": running_litres},
            )
        if period.total_cost_including_sewerage is not None:
            cost = float(period.total_cost_including_sewerage)
            running_cost += cost
            cost_stats.append(
                {"start": start, "state": cost, "sum": running_cost},
            )

    if consumption_stats:
        async_add_external_statistics(
            hass,
            _metadata(
                consumption_id,
                "Yorkshire Water monthly consumption",
                UnitOfVolume.LITERS,
            ),
            consumption_stats,
        )
    if cost_stats:
        async_add_external_statistics(
            hass,
            _metadata(cost_id, "Yorkshire Water monthly cost", _COST_UNIT),
            cost_stats,
        )
    LOGGER.debug(
        "Imported YW monthly statistics for %s: %d consumption, %d cost buckets",
        ref,
        len(consumption_stats),
        len(cost_stats),
    )
