"""Sensor entities for the Yorkshire Water integration.

Sensors are per-property: each property on the customer's account
gets its own device with the standard set of sensors. For accounts
with a single property the result is identical to v0.4.x.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util
from pyyorkshirewater import MeterStatus

from .const import (
    ATTR_ALARM_DETAILS,
    ATTR_LAST_UPDATED,
    ATTR_METER_REFERENCE,
    ATTR_METER_STATUS,
)
from .entity import YorkshireWaterEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import YorkshireWaterConfigEntry
    from .coordinator import PropertyData, YorkshireWaterCoordinator


@dataclass(frozen=True, kw_only=True)
class YorkshireWaterSensorEntityDescription(SensorEntityDescription):
    """SensorEntityDescription with a per-property value extractor."""

    value_fn: Callable[[PropertyData], Any]
    available_fn: Callable[[PropertyData], bool] | None = None


def _sorted_dated_points(data: PropertyData) -> list[Any]:
    """Return daily points with a date, sorted in ascending date order."""
    return sorted(
        (p for p in data.daily_points if getattr(p, "point_date", None) is not None),
        key=lambda p: p.point_date,
    )


# How many trailing days the "last 8 days" sensor covers. The
# coordinator now fetches a wider daily window (~35 days) to feed the
# daily statistics backfill, so this sensor filters back down to its
# own window rather than summing whatever was fetched.
_WINDOW_DAYS = 8


def _window_sum(data: PropertyData) -> float | None:
    """Return total consumption over the last `_WINDOW_DAYS` days, in litres."""
    if not data.daily_points:
        return None
    cutoff = dt_util.now().date() - timedelta(days=_WINDOW_DAYS)
    total = 0.0
    found = False
    for point in data.daily_points:
        point_date = getattr(point, "point_date", None)
        litres = getattr(point, "total_consumption_litres", None)
        if point_date is None or point_date <= cutoff or litres is None:
            continue
        total += float(litres)
        found = True
    return total if found else None


def _point_for_date(data: PropertyData, target: date) -> Any | None:
    """Return the daily point whose date matches `target`, or None.

    Strict calendar-date matching. YW only ever publish a COMPLETE
    daily total, so the freshest day they can deliver is yesterday
    (and only once their pipeline catches up, which is usually a day
    later). There is deliberately no "today" sensor: a finished daily
    total cannot exist for a day that has not ended.
    """
    for point in _sorted_dated_points(data):
        if point.point_date == target:
            return point
    return None


def _yesterday_consumption(data: PropertyData) -> float | None:
    """Return yesterday's consumption (litres). Unavailable until YW delivers it."""
    point = _point_for_date(data, dt_util.now().date() - timedelta(days=1))
    return getattr(point, "total_consumption_litres", None) if point else None


def _has_yesterday_reading(data: PropertyData) -> bool:
    """True only when YW have delivered a reading for yesterday's calendar date."""
    if not _live_only(data):
        return False
    return _point_for_date(data, dt_util.now().date() - timedelta(days=1)) is not None


def _last_reading_time(data: PropertyData) -> datetime | None:
    """Return the date Yorkshire Water last read the meter.

    This is the date YW logged a reading, NOT when the integration last
    polled. The API exposes `latestDataDate` as a date only (no time of
    day), so we anchor it to midnight UTC; do not read precision into
    the time component. Prefers `current_consumption.latest_data_date`
    (always populated for a live meter, single API call), falls back to
    the most recent daily-series point for installs where daily data is
    available.
    """
    if data.current_consumption and data.current_consumption.latest_data_date:
        d = data.current_consumption.latest_data_date
        return datetime(d.year, d.month, d.day, tzinfo=UTC)
    points = _sorted_dated_points(data)
    if not points:
        return None
    point_date = points[-1].point_date
    return datetime(point_date.year, point_date.month, point_date.day, tzinfo=UTC)


def _last_update_time(data: PropertyData) -> datetime | None:
    """Return when YW's aggregation pipeline last refreshed the summary."""
    if not data.current_consumption or not data.current_consumption.latest_update_date:
        return None
    d = data.current_consumption.latest_update_date
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _meter_reference(data: PropertyData) -> str | None:
    """Return the meter reference, if any."""
    if data.meter_details is None:
        return None
    return data.meter_details.meter_reference


def _yesterday_cost(data: PropertyData) -> float | None:
    """Return yesterday's full water bill (inc. sewerage) if YW have a reading."""
    point = _point_for_date(data, dt_util.now().date() - timedelta(days=1))
    return getattr(point, "total_cost", None) if point else None


def _live_only(data: PropertyData) -> bool:
    return data.meter_status is MeterStatus.LIVE


def _has_meter(data: PropertyData) -> bool:
    return bool(_meter_reference(data))


def _has_usage(data: PropertyData) -> bool:
    """Available when we have at least the current month's usage summary."""
    return data.meter_status is MeterStatus.LIVE and bool(data.usage_periods)


def _has_prev_usage(data: PropertyData) -> bool:
    """Available when we have at least two months' data (this and last)."""
    return data.meter_status is MeterStatus.LIVE and len(data.usage_periods) >= 2


def _has_yearly(data: PropertyData) -> bool:
    return data.meter_status is MeterStatus.LIVE and data.yearly_consumption is not None


# Monthly summary value functions: usage_periods is ordered most-recent
# first by the API, so [0] is the current month and [1] is the previous.

def _this_month_consumption(data: PropertyData) -> float | None:
    return data.usage_periods[0].total_consumption_litres if data.usage_periods else None


def _this_month_clean_cost(data: PropertyData) -> float | None:
    return data.usage_periods[0].clean_water_cost if data.usage_periods else None


def _this_month_sewerage_cost(data: PropertyData) -> float | None:
    return data.usage_periods[0].sewerage_cost if data.usage_periods else None


def _this_month_total_cost(data: PropertyData) -> float | None:
    return (
        data.usage_periods[0].total_cost_including_sewerage
        if data.usage_periods else None
    )


def _last_month_consumption(data: PropertyData) -> float | None:
    return (
        data.usage_periods[1].total_consumption_litres
        if len(data.usage_periods) >= 2 else None
    )


def _last_month_total_cost(data: PropertyData) -> float | None:
    return (
        data.usage_periods[1].total_cost_including_sewerage
        if len(data.usage_periods) >= 2 else None
    )


# Year-to-date value functions

def _ytd_consumption(data: PropertyData) -> float | None:
    return data.yearly_consumption.total_consumption_litres if data.yearly_consumption else None


def _ytd_total_cost(data: PropertyData) -> float | None:
    return data.yearly_consumption.total_cost if data.yearly_consumption else None


def _monthly_avg_consumption(data: PropertyData) -> float | None:
    return data.yearly_consumption.monthly_litres_average if data.yearly_consumption else None


def _monthly_avg_cost(data: PropertyData) -> float | None:
    return data.yearly_consumption.monthly_cost_average if data.yearly_consumption else None


# Continuous-flow alarm detail value functions

def _continuous_flow_rate(data: PropertyData) -> float | None:
    cc = data.current_consumption
    if not cc or not cc.continuous_flow_alarm_details:
        return None
    return cc.continuous_flow_alarm_details[0].continuous_flow_l_per_h


def _continuous_flow_cost(data: PropertyData) -> float | None:
    cc = data.current_consumption
    if not cc or not cc.continuous_flow_alarm_details:
        return None
    return cc.continuous_flow_alarm_details[0].cost_per_day


SENSORS: tuple[YorkshireWaterSensorEntityDescription, ...] = (
    YorkshireWaterSensorEntityDescription(
        key="window_consumption",
        translation_key="window_consumption",
        # Name reflects the actual window the coordinator fetches (8
        # days), not a vague "Recent". The same data feeds the
        # cumulative sensors used by the HA Energy dashboard, so
        # users browsing entities should know which timeframe this
        # one covers.
        name="Consumption (last 8 days)",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=_window_sum,
        available_fn=_live_only,
    ),
    # NOTE: there is deliberately no "consumption today" / "cost today"
    # sensor. YW only publish COMPLETE daily totals, so a figure for the
    # current (unfinished) day cannot exist - by the time the day's total
    # is settled, the calendar has rolled over and it is "yesterday". A
    # "today" sensor would therefore be permanently unavailable.
    YorkshireWaterSensorEntityDescription(
        key="consumption_yesterday",
        translation_key="consumption_yesterday",
        name="Consumption yesterday",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=1,
        value_fn=_yesterday_consumption,
        available_fn=_has_yesterday_reading,
    ),
    YorkshireWaterSensorEntityDescription(
        key="cost_yesterday",
        translation_key="cost_yesterday",
        name="Cost yesterday",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=_yesterday_cost,
        available_fn=_has_yesterday_reading,
    ),
    YorkshireWaterSensorEntityDescription(
        key="last_reading_time",
        translation_key="last_reading_time",
        name="Last YW reading date",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_reading_time,
        available_fn=_live_only,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    YorkshireWaterSensorEntityDescription(
        key="last_update_time",
        name="Last update time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_update_time,
        available_fn=_live_only,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    YorkshireWaterSensorEntityDescription(
        key="meter_reference",
        translation_key="meter_reference",
        name="Meter reference",
        value_fn=_meter_reference,
        available_fn=_has_meter,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Monthly summary sensors — populated from /your-usage
    YorkshireWaterSensorEntityDescription(
        key="consumption_this_month",
        name="Consumption this month",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=_this_month_consumption,
        available_fn=_has_usage,
    ),
    YorkshireWaterSensorEntityDescription(
        key="cost_this_month_clean_water",
        name="Clean water cost this month",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=_this_month_clean_cost,
        available_fn=_has_usage,
    ),
    YorkshireWaterSensorEntityDescription(
        key="cost_this_month_sewerage",
        name="Sewerage cost this month",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=_this_month_sewerage_cost,
        available_fn=_has_usage,
    ),
    YorkshireWaterSensorEntityDescription(
        key="cost_this_month_total",
        name="Total cost this month",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=_this_month_total_cost,
        available_fn=_has_usage,
    ),
    YorkshireWaterSensorEntityDescription(
        key="consumption_last_month",
        name="Consumption last month",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=_last_month_consumption,
        available_fn=_has_prev_usage,
    ),
    YorkshireWaterSensorEntityDescription(
        key="cost_last_month_total",
        name="Total cost last month",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=_last_month_total_cost,
        available_fn=_has_prev_usage,
    ),
    # Year-to-date sensors — populated from /yearly-consumption
    YorkshireWaterSensorEntityDescription(
        key="consumption_year_to_date",
        name="Consumption year to date",
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=_ytd_consumption,
        available_fn=_has_yearly,
    ),
    YorkshireWaterSensorEntityDescription(
        key="cost_year_to_date",
        name="Cost year to date",
        device_class=SensorDeviceClass.MONETARY,
        # MONETARY only permits state_class=total or no state_class. The
        # YTD cost is monotonic-ish (resets each calendar year) so TOTAL
        # is the right choice — total_increasing would imply
        # never-resets.
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=_ytd_total_cost,
        available_fn=_has_yearly,
    ),
    YorkshireWaterSensorEntityDescription(
        key="average_monthly_consumption",
        name="Average monthly consumption",
        # WATER device class only permits state_class=total /
        # total_increasing / none. Averages are neither. Drop the
        # device_class so we can use state_class=measurement, which
        # lets HA's long-term-stats engine track how the average
        # shifts as more months land. The unit (litres) survives,
        # we just lose the "water drop" icon - acceptable trade.
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=_monthly_avg_consumption,
        available_fn=_has_yearly,
    ),
    YorkshireWaterSensorEntityDescription(
        key="average_monthly_cost",
        name="Average monthly cost",
        # MONETARY only permits state_class=total / none. Same
        # trade-off as the consumption average above: drop the
        # device_class so the MEASUREMENT state class can stand.
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=_monthly_avg_cost,
        available_fn=_has_yearly,
    ),
    # Continuous-flow alarm detail sensors. The API always returns one
    # entry in `currentContinuousFlowAlarmDetails` with zeros when
    # there's no leak. Showing the zero baseline continuously means any
    # non-zero blip on the graph is an early-warning signal even
    # before YW's pipeline officially flips the alarm.
    YorkshireWaterSensorEntityDescription(
        key="continuous_flow_rate",
        name="Continuous flow rate",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="L/h",
        suggested_display_precision=1,
        value_fn=_continuous_flow_rate,
        available_fn=_live_only,
    ),
    YorkshireWaterSensorEntityDescription(
        key="continuous_flow_cost_per_day",
        name="Continuous flow cost per day",
        device_class=SensorDeviceClass.MONETARY,
        # MONETARY does not permit state_class=measurement; leave it
        # unset so HA renders the value without trying to bucket it
        # into long-term stats.
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=_continuous_flow_cost,
        available_fn=_live_only,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register sensor entities for every property on the account."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    entities: list[SensorEntity] = []
    if data is not None:
        for property_data in data.properties:
            for desc in SENSORS:
                entities.append(
                    YorkshireWaterSensor(coordinator, property_data, desc),
                )
            entities.append(
                YorkshireWaterCumulativeSensor(coordinator, property_data),
            )
            entities.append(
                YorkshireWaterCumulativeCostSensor(coordinator, property_data),
            )
            entities.append(
                YorkshireWaterMeterStatusSensor(coordinator, property_data),
            )
    async_add_entities(entities)


class YorkshireWaterMeterStatusSensor(YorkshireWaterEntity, SensorEntity):
    """Always-available, human-readable meter readiness state.

    Yorkshire Water is rolling smart meters out across the region from
    2025 to 2030, so most accounts go through a `no_meter` and
    `pending_activation` phase before they reach `live`. While in
    those phases the consumption sensors are unavailable - which can
    look like the integration is broken when in fact it is just
    waiting on the meter being commissioned upstream. This sensor is
    always populated and tells the user, in words, where their meter
    is in the rollout. Pair it with the consumption tiles on a
    dashboard and the unavailable state stops looking like a fault.
    """

    _attr_translation_key = "meter_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = [
        MeterStatus.NO_METER.value,
        MeterStatus.PENDING_ACTIVATION.value,
        MeterStatus.LIVE.value,
    ]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: YorkshireWaterCoordinator,
        property_data: PropertyData,
    ) -> None:
        """Bind to the property's device alongside the other sensors."""
        super().__init__(
            coordinator,
            property_data=property_data,
            key="meter_status",
        )

    @property
    def available(self) -> bool:
        """Always available - this sensor is the readiness affordance."""
        return self.coordinator.data is not None

    @property
    def native_value(self) -> str | None:
        """Return one of `no_meter`, `pending_activation`, `live`."""
        snapshot = self.property_data()
        if snapshot is None:
            return None
        return snapshot.meter_status.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the property address and account reference.

        The meter-status sensor is always available and there is one
        per property, so it is the natural carrier for property-level
        metadata. Dashboards read the `address` attribute to render a
        per-property heading without hard-coding the address.
        """
        snapshot = self.property_data()
        if snapshot is None or snapshot.property is None:
            return {}
        attrs: dict[str, Any] = {}
        address = snapshot.property.address
        if address is not None:
            formatted = address.formatted()
            if formatted:
                attrs["address"] = formatted
        if snapshot.property.display_account_reference:
            attrs["account_reference"] = snapshot.property.display_account_reference
        return attrs


class YorkshireWaterSensor(YorkshireWaterEntity, SensorEntity):
    """Generic sensor backed by a SensorEntityDescription with a value_fn."""

    entity_description: YorkshireWaterSensorEntityDescription

    def __init__(
        self,
        coordinator: YorkshireWaterCoordinator,
        property_data: PropertyData,
        description: YorkshireWaterSensorEntityDescription,
    ) -> None:
        """Wire the description and shared device info."""
        super().__init__(
            coordinator,
            property_data=property_data,
            key=description.key,
        )
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Hide the sensor while the underlying readiness gate is closed."""
        if not super().available:
            return False
        snapshot = self.property_data()
        if snapshot is None:
            return False
        check = self.entity_description.available_fn
        return check(snapshot) if check else True

    @property
    def native_value(self) -> Any:
        """Return the most recent value computed from the property snapshot."""
        snapshot = self.property_data()
        if snapshot is None:
            return None
        return self.entity_description.value_fn(snapshot)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose meter status, reference, and alarm details."""
        snapshot = self.property_data()
        attrs: dict[str, Any] = {}
        if snapshot is not None:
            attrs[ATTR_METER_STATUS] = snapshot.meter_status.value
            if snapshot.meter_details:
                attrs[ATTR_METER_REFERENCE] = snapshot.meter_details.meter_reference
            if snapshot.current_consumption is not None:
                attrs[ATTR_ALARM_DETAILS] = [
                    alarm.raw
                    for alarm in snapshot.current_consumption.continuous_flow_alarm_details
                ]
            attrs[ATTR_LAST_UPDATED] = datetime.now(UTC).isoformat()
        return attrs


class YorkshireWaterCumulativeSensor(
    YorkshireWaterEntity,
    SensorEntity,
    RestoreEntity,
):
    """Monotonic cumulative water consumption for a single property."""

    _attr_translation_key = "cumulative_consumption"
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.LITERS
    _attr_suggested_display_precision = 0
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: YorkshireWaterCoordinator,
        property_data: PropertyData,
    ) -> None:
        """Configure the cumulative sensor."""
        super().__init__(
            coordinator,
            property_data=property_data,
            key="cumulative_consumption",
        )
        self._daily_totals: dict[str, float] = {}

    async def async_added_to_hass(self) -> None:
        """Restore the per-day dict from previous attributes on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes:
            persisted = last_state.attributes.get("daily_totals")
            if isinstance(persisted, dict):
                for k, v in persisted.items():
                    try:
                        self._daily_totals[str(k)] = float(v)
                    except (TypeError, ValueError):
                        continue
        self._absorb_coordinator_points()

    def _absorb_coordinator_points(self) -> None:
        """Merge the latest daily points into our per-day dict."""
        snapshot = self.property_data()
        if snapshot is None or snapshot.meter_status is not MeterStatus.LIVE:
            return
        for point in snapshot.daily_points:
            if point.point_date is None or point.total_consumption_litres is None:
                continue
            iso_date = point.point_date.isoformat()
            new_val = float(point.total_consumption_litres)
            existing = self._daily_totals.get(iso_date)
            if existing is None or new_val > existing:
                self._daily_totals[iso_date] = new_val

    @property
    def available(self) -> bool:
        """Available once we have any data."""
        if not super().available:
            return False
        return bool(self._daily_totals) or self.property_data() is not None

    @property
    def native_value(self) -> float | None:
        """Sum the per-day dict and return as a monotonic cumulative total."""
        self._absorb_coordinator_points()
        if not self._daily_totals:
            return None
        return round(sum(self._daily_totals.values()), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Persist the per-day dict so it survives restarts."""
        return {
            "daily_totals": dict(self._daily_totals),
            "tracked_days": len(self._daily_totals),
        }


class YorkshireWaterCumulativeCostSensor(
    YorkshireWaterEntity,
    SensorEntity,
    RestoreEntity,
):
    """Monotonic cumulative water bill cost in pounds for a single property."""

    _attr_translation_key = "cumulative_cost"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "GBP"
    _attr_suggested_display_precision = 2
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: YorkshireWaterCoordinator,
        property_data: PropertyData,
    ) -> None:
        """Configure the cumulative cost sensor."""
        super().__init__(
            coordinator,
            property_data=property_data,
            key="cumulative_cost",
        )
        self._daily_costs: dict[str, float] = {}

    async def async_added_to_hass(self) -> None:
        """Restore the per-day cost dict on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes:
            persisted = last_state.attributes.get("daily_costs")
            if isinstance(persisted, dict):
                for k, v in persisted.items():
                    try:
                        self._daily_costs[str(k)] = float(v)
                    except (TypeError, ValueError):
                        continue
        self._absorb_coordinator_points()

    def _absorb_coordinator_points(self) -> None:
        """Merge the latest daily costs into our per-day dict."""
        snapshot = self.property_data()
        if snapshot is None or snapshot.meter_status is not MeterStatus.LIVE:
            return
        for point in snapshot.daily_points:
            if point.point_date is None or point.total_cost is None:
                continue
            iso_date = point.point_date.isoformat()
            new_val = float(point.total_cost)
            existing = self._daily_costs.get(iso_date)
            if existing is None or new_val > existing:
                self._daily_costs[iso_date] = new_val

    @property
    def available(self) -> bool:
        """Available once we have any cost data."""
        if not super().available:
            return False
        return bool(self._daily_costs) or self.property_data() is not None

    @property
    def native_value(self) -> float | None:
        """Sum the per-day cost dict for a monotonic running total."""
        self._absorb_coordinator_points()
        if not self._daily_costs:
            return None
        return round(sum(self._daily_costs.values()), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Persist the per-day cost dict across restarts."""
        return {
            "daily_costs": dict(self._daily_costs),
            "tracked_days": len(self._daily_costs),
        }
