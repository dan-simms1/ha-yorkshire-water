"""Import Yorkshire Water consumption + cost into HA long-term statistics.

This is the heart of the integration (v3.0): YW smart water meters
report ~daily with a multi-day lag, so the right home for the data is
HA's long-term statistics, dated to the reading, not "live" sensors.

We inject dedicated EXTERNAL statistics, one set per property, keyed on
the account reference:

    yorkshire_water:daily_consumption_<account>     (litres, per day)
    yorkshire_water:daily_cost_<account>            (GBP,    per day)
    yorkshire_water:monthly_consumption_<account>   (litres, per month)
    yorkshire_water:monthly_cost_<account>          (GBP,    per month)

Each bucket's `state` is that period's own total (charts use
`stat_types: [state]`); `sum` is an absolute running cumulative from
the first reading we have ever seen, so the statistic is also usable as
a Home Assistant Energy Dashboard source (Energy reads `sum` deltas and
requires it to be globally monotonic, not restarted each import).

To keep `sum` globally monotonic without re-fetching all history, we
persist a small per-property ledger (`{period-start: value}`) in HA
storage. Each poll merges the freshly-fetched window into the ledger,
then recomputes the absolute running sum across the whole ledger and
re-imports. Re-importing a period overwrites it, so revised YW
estimates self-correct; the ledger only grows (~365 rows/year), so the
absolute sum never shifts just because the fetch window slid forward.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfVolume
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER

try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_NONE: Any | None = StatisticMeanType.NONE
except ImportError:  # HA < 2025.10
    _MEAN_NONE = None

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import PropertyData

_COST_UNIT = "GBP"
_VOLUME_UNIT_CLASS = "volume"
_LEDGER_VERSION = 1


def _ledger_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Persistent per-entry ledger of every (period -> value) ever seen.

    Kept separate from the snapshot/auth stores. Shape:
        { "<account>": {
            "daily":   {"YYYY-MM-DD": [litres, cost_or_null]},
            "monthly": {"YYYY-MM-01": [litres, cost_or_null]},
        } }
    """
    return Store(hass, _LEDGER_VERSION, f"{DOMAIN}.{entry_id}.stat_ledger")


async def async_remove_statistics_ledger(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the persisted statistics ledger when an entry is removed.

    Holds the account reference and dated consumption/cost history, so
    it must not linger after the user deletes the integration.
    """
    await _ledger_store(hass, entry_id).async_remove()


def _day_start(value: date) -> Any:
    return dt_util.as_utc(dt_util.start_of_local_day(value))


def _metadata(statistic_id: str, name: str, unit: str) -> dict[str, Any]:
    """StatisticMetaData for one external statistic.

    Litres get unit_class="volume" so HA offers unit conversion and the
    Energy water source accepts them; GBP cost stats get unit_class=None
    (no converter exists for currency) which also skips the recorder's
    unit validation.
    """
    meta: dict[str, Any] = {
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "name": name,
        "unit_of_measurement": unit,
        "unit_class": _VOLUME_UNIT_CLASS if unit == UnitOfVolume.LITERS else None,
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
    series: list[tuple[Any, float]],
) -> int:
    """Import one external statistic from a full, sorted (start, value) series.

    `state` is each period's value; `sum` is the absolute running total
    across the whole series (globally monotonic). Returns bucket count.
    """
    if not series:
        return 0
    stats: list[dict[str, Any]] = []
    running = 0.0
    for start, value in series:
        running += value
        stats.append({"start": start, "state": value, "sum": running})
    async_add_external_statistics(hass, _metadata(statistic_id, name, unit), stats)
    return len(stats)


def _merge(section: dict[str, Any], key: str, litres: float, cost: float | None) -> None:
    """Upsert one (litres, cost) bucket, preserving a prior non-null cost.

    A re-read that has litres but a null cost must not wipe a cost we
    already recorded for that period, otherwise the cost series would
    lose the day. Litres are always taken from the fresh read (we never
    reach here with null litres).
    """
    prior = section.get(key)
    if cost is None and isinstance(prior, list) and len(prior) > 1 and prior[1] is not None:
        cost = prior[1]
    section[key] = [litres, cost]


def _merge_daily(acct: dict[str, Any], property_data: PropertyData) -> None:
    """Fold the fetched daily window into the persisted daily ledger.

    A day that comes back flagged missing or with null litres is left
    as-is in the ledger: a transmission gap is not evidence of zero
    consumption, so the last real value is kept (last-known-good).
    """
    daily = acct.setdefault("daily", {})
    for point in property_data.daily_points:
        if point.point_date is None or getattr(point, "is_missing", False):
            continue
        if point.total_consumption_litres is None:
            continue
        cost = (
            float(point.total_cost_including_sewerage)
            if point.total_cost_including_sewerage is not None
            else None
        )
        _merge(daily, point.point_date.isoformat(), float(point.total_consumption_litres), cost)


def _merge_monthly(acct: dict[str, Any], property_data: PropertyData) -> None:
    """Fold the yearly-consumption monthly breakdown into the ledger."""
    yearly = property_data.yearly_consumption
    if yearly is None or not yearly.year or not yearly.monthly_consumption:
        return
    monthly = acct.setdefault("monthly", {})
    for period in yearly.monthly_consumption:
        if not period.month or not period.month.isdigit():
            continue
        month = int(period.month)
        if not 1 <= month <= 12:
            continue
        if period.total_consumption_litres is None:
            continue
        key = date(yearly.year, month, 1).isoformat()
        cost = (
            float(period.total_cost_including_sewerage)
            if period.total_cost_including_sewerage is not None
            else None
        )
        _merge(monthly, key, float(period.total_consumption_litres), cost)


def _series(ledger_section: dict[str, list[Any]], index: int) -> list[tuple[Any, float]]:
    """Build a sorted (start, value) series from a ledger section.

    `index` selects litres (0) or cost (1); entries whose chosen value
    is null are skipped.
    """
    out: list[tuple[Any, float]] = []
    for iso, values in sorted(ledger_section.items()):
        if not isinstance(values, list):
            continue
        value = values[index] if index < len(values) else None
        if value is None:
            continue
        try:
            out.append((_day_start(date.fromisoformat(iso)), float(value)))
        except (ValueError, TypeError):
            continue
    return out


async def async_import_property_statistics(
    hass: HomeAssistant,
    entry_id: str,
    property_data: PropertyData,
) -> None:
    """Merge this poll's data into the ledger and (re)import all statistics.

    Awaitable: loads/saves the persisted ledger. Skips quietly when the
    property has no reference. Never raises for empty data.
    """
    ref = property_data.property.display_account_reference
    if not ref:
        return

    store = _ledger_store(hass, entry_id)
    raw = await store.async_load()
    ledger: dict[str, Any] = raw if isinstance(raw, dict) else {}
    acct = ledger.get(ref)
    if not isinstance(acct, dict):
        acct = ledger[ref] = {"daily": {}, "monthly": {}}
    if not isinstance(acct.get("daily"), dict):
        acct["daily"] = {}
    if not isinstance(acct.get("monthly"), dict):
        acct["monthly"] = {}

    _merge_daily(acct, property_data)
    _merge_monthly(acct, property_data)
    await store.async_save(ledger)

    n_dc = _emit(
        hass,
        f"{DOMAIN}:daily_consumption_{ref}",
        "Yorkshire Water daily consumption",
        UnitOfVolume.LITERS,
        _series(acct["daily"], 0),
    )
    _emit(
        hass,
        f"{DOMAIN}:daily_cost_{ref}",
        "Yorkshire Water daily cost",
        _COST_UNIT,
        _series(acct["daily"], 1),
    )
    n_mc = _emit(
        hass,
        f"{DOMAIN}:monthly_consumption_{ref}",
        "Yorkshire Water monthly consumption",
        UnitOfVolume.LITERS,
        _series(acct["monthly"], 0),
    )
    _emit(
        hass,
        f"{DOMAIN}:monthly_cost_{ref}",
        "Yorkshire Water monthly cost",
        _COST_UNIT,
        _series(acct["monthly"], 1),
    )
    LOGGER.debug(
        "Imported YW statistics for %s: %d daily, %d monthly buckets",
        ref,
        n_dc,
        n_mc,
    )
