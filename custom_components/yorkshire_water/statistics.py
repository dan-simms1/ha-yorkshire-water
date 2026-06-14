"""Backfill Yorkshire Water totals into HA long-term statistics.

A native `statistics-graph` card can only chart periods the recorder
already has data for. The live sensors only begin recording when the
integration is installed, and the cumulative sensor's per-period
`change` is unreliable across restarts and the sliding daily fetch
window. So a daily or monthly chart built off them is wrong: empty
before install, and prone to artefacts like a line sloping to zero on
the current (incomplete) day.

This module injects Yorkshire Water's own authoritative totals as
dedicated EXTERNAL statistics, one set per property, so the charts show
real bars from the very first poll - including history from before the
integration existed. We deliberately do NOT backfill the live sensors'
own recorder rows: that would claim the entities held values before
they existed, and couple historical data to entity ids derived from the
(mutable) property address.

Statistic ids are keyed on the stable `display_account_reference`:
    yorkshire_water:daily_consumption_<ref>     (litres, per calendar day)
    yorkshire_water:daily_cost_<ref>            (GBP,    per calendar day)
    yorkshire_water:monthly_consumption_<ref>   (litres, per calendar month)
    yorkshire_water:monthly_cost_<ref>          (GBP,    per calendar month)

Each bucket's `state` carries that period's total (what the chart plots
with `stat_types: [state]`); `sum` carries a running cumulative purely
to satisfy `has_sum` metadata. Re-importing the same (statistic_id,
start) on every poll is idempotent - the recorder updates the existing
row - so revised YW estimates self-correct.
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


def _day_start(value: date) -> Any:
    """Return the UTC datetime for midnight local on the given date."""
    return dt_util.as_utc(dt_util.start_of_local_day(value))


def _month_start(year: int, month: int) -> Any:
    """Return the UTC datetime for midnight local on the 1st of the month."""
    return _day_start(date(year, month, 1))


def _metadata(statistic_id: str, name: str, unit: str) -> dict[str, Any]:
    """Build StatisticMetaData for one external statistic.

    Typed as a plain dict so the optional `mean_type` key can be
    omitted on older HA cores without tripping the TypedDict.

    `unit_class` is set to None deliberately: it is present (so newer
    cores do not warn about its absence) but None skips the recorder's
    unit-conversion validation, which would otherwise reject "GBP" and
    is unnecessary for a standalone bar chart.
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


def _emit(
    hass: HomeAssistant,
    statistic_id: str,
    name: str,
    unit: str,
    buckets: list[tuple[Any, float]],
) -> int:
    """Import one external statistic from (start, value) buckets.

    `state` is each period's own total; `sum` is a running cumulative
    so the statistic satisfies has_sum metadata. Returns the bucket
    count for logging. Does nothing for an empty bucket list.
    """
    if not buckets:
        return 0
    stats: list[dict[str, Any]] = []
    running = 0.0
    for start, value in buckets:
        running += value
        stats.append({"start": start, "state": value, "sum": running})
    async_add_external_statistics(hass, _metadata(statistic_id, name, unit), stats)
    return len(stats)


def async_import_property_statistics(
    hass: HomeAssistant,
    property_data: PropertyData,
) -> None:
    """Import a property's daily + monthly consumption and cost statistics.

    Safe to call on every coordinator refresh. Skips quietly when the
    property has no reference yet. Never raises for empty data; the
    caller wraps this anyway.
    """
    ref = property_data.property.display_account_reference
    if not ref:
        return

    _import_daily(hass, property_data, ref)
    _import_monthly(hass, property_data, ref)


def _import_daily(hass: HomeAssistant, property_data: PropertyData, ref: str) -> None:
    """Import per-day consumption + cost buckets from the daily series."""
    points = sorted(
        (
            point
            for point in property_data.daily_points
            if getattr(point, "point_date", None) is not None
            # A "missing" day is one YW has no real reading for; skip it
            # so the chart shows an honest gap rather than a false zero.
            and not getattr(point, "is_missing", False)
        ),
        key=lambda point: point.point_date,
    )

    consumption: list[tuple[Any, float]] = []
    cost: list[tuple[Any, float]] = []
    for point in points:
        start = _day_start(point.point_date)
        if point.total_consumption_litres is not None:
            consumption.append((start, float(point.total_consumption_litres)))
        if point.total_cost_including_sewerage is not None:
            cost.append((start, float(point.total_cost_including_sewerage)))

    n_c = _emit(
        hass,
        f"{DOMAIN}:daily_consumption_{ref}",
        "Yorkshire Water daily consumption",
        UnitOfVolume.LITERS,
        consumption,
    )
    n_k = _emit(
        hass,
        f"{DOMAIN}:daily_cost_{ref}",
        "Yorkshire Water daily cost",
        _COST_UNIT,
        cost,
    )
    LOGGER.debug(
        "Imported YW daily statistics for %s: %d consumption, %d cost buckets",
        ref,
        n_c,
        n_k,
    )


def _import_monthly(hass: HomeAssistant, property_data: PropertyData, ref: str) -> None:
    """Import per-month consumption + cost buckets from yearly-consumption."""
    yearly = property_data.yearly_consumption
    if yearly is None or not yearly.year or not yearly.monthly_consumption:
        return

    months = sorted(
        (
            period
            for period in yearly.monthly_consumption
            if period.month and period.month.isdigit() and 1 <= int(period.month) <= 12
        ),
        key=lambda period: int(period.month),
    )

    consumption: list[tuple[Any, float]] = []
    cost: list[tuple[Any, float]] = []
    for period in months:
        start = _month_start(yearly.year, int(period.month))
        if period.total_consumption_litres is not None:
            consumption.append((start, float(period.total_consumption_litres)))
        if period.total_cost_including_sewerage is not None:
            cost.append((start, float(period.total_cost_including_sewerage)))

    n_c = _emit(
        hass,
        f"{DOMAIN}:monthly_consumption_{ref}",
        "Yorkshire Water monthly consumption",
        UnitOfVolume.LITERS,
        consumption,
    )
    n_k = _emit(
        hass,
        f"{DOMAIN}:monthly_cost_{ref}",
        "Yorkshire Water monthly cost",
        _COST_UNIT,
        cost,
    )
    LOGGER.debug(
        "Imported YW monthly statistics for %s: %d consumption, %d cost buckets",
        ref,
        n_c,
        n_k,
    )
