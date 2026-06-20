"""Sensor entities for the Yorkshire Water integration.

Sensors are per-property: each property on the customer's account
gets its own device.

Design note (v3.0): Yorkshire Water smart water meters report roughly
once a day and their per-day breakdown lands ~2 days late. That data
shape does not suit "live" daily/yesterday sensors (they would be
stale or unavailable). So the per-day and per-month history lives in
HA long-term statistics, backfilled from YW's own dated data (see
statistics.py), and the dashboard charts read those. The live sensors
here are limited to genuinely-current values: meter status, the
leak-detection figures, month-to-date and year-to-date totals, a
single "most recent reading" diagnostic, and identifiers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.util import dt as dt_util
from pyyorkshirewater import MeterStatus

from .const import (
    ATTR_ALARM_DETAILS,
    ATTR_LAST_UPDATED,
    ATTR_METER_REFERENCE,
    ATTR_METER_STATUS,
    UPDATE_STATUSES,
    format_account_number,
)
from .entity import YorkshireWaterEntity, YorkshireWaterEntryEntity

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


def _latest_point(data: PropertyData) -> Any | None:
    """Return the most recent REAL daily point Yorkshire Water has delivered.

    Skips placeholder days that are flagged missing or carry no litres,
    so the "latest reading" reflects the freshest actual measurement
    rather than a trailing gap.
    """
    for point in reversed(_sorted_dated_points(data)):
        if getattr(point, "is_missing", False):
            continue
        if point.total_consumption_litres is None:
            continue
        return point
    return None


# --- live-current value functions -------------------------------------------


def _live_only(data: PropertyData) -> bool:
    return data.meter_status is MeterStatus.LIVE


def _has_meter(data: PropertyData) -> bool:
    return bool(data.meter_details and data.meter_details.meter_reference)


def _has_usage(data: PropertyData) -> bool:
    """Available when we have at least the current month's usage summary."""
    return data.meter_status is MeterStatus.LIVE and bool(data.usage_periods)


def _has_yearly(data: PropertyData) -> bool:
    return data.meter_status is MeterStatus.LIVE and data.yearly_consumption is not None


def _has_daily(data: PropertyData) -> bool:
    return data.meter_status is MeterStatus.LIVE and _latest_point(data) is not None


def _meter_reference(data: PropertyData) -> str | None:
    if data.meter_details is None:
        return None
    return data.meter_details.meter_reference


def _latest_reading_date(data: PropertyData) -> date | None:
    """Return the date of the newest REAL daily reading we have.

    Sourced from the daily breakdown (`_latest_point`), NOT YW's
    `latest_data_date` marker. YW's marker runs ~1 day ahead of the
    published daily total, so it would claim a date we have no
    consumption figure for. This returns the honest "newest data we
    actually hold" date, matching `latest_daily_consumption`'s
    reading_date.
    """
    point = _latest_point(data)
    if point is None or point.point_date is None:
        return None
    return point.point_date


def _yw_data_refreshed(data: PropertyData) -> date | None:
    """Return the date YW's systems last refreshed this account (YW's clock)."""
    if not data.current_consumption or not data.current_consumption.latest_update_date:
        return None
    return data.current_consumption.latest_update_date


def _latest_daily_consumption(data: PropertyData) -> float | None:
    """Most recent daily consumption YW has delivered, in litres.

    This is a diagnostic "freshest known reading" value - NOT a
    calendar-dated sensor, and deliberately has no state_class so it is
    not recorded into long-term statistics (the dated daily history
    comes exclusively from the backfilled external statistics). The
    reading's actual date and lag are exposed as attributes.
    """
    point = _latest_point(data)
    return getattr(point, "total_consumption_litres", None) if point else None


# --- month-to-date / year-to-date (genuine current totals) ------------------


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


def _ytd_consumption(data: PropertyData) -> float | None:
    return data.yearly_consumption.total_consumption_litres if data.yearly_consumption else None


def _ytd_total_cost(data: PropertyData) -> float | None:
    return data.yearly_consumption.total_cost if data.yearly_consumption else None


# --- continuous-flow (leak) -------------------------------------------------


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
    # Most recent daily reading. Diagnostic, no state_class: the dated
    # per-day history is in long-term statistics, not this sensor.
    YorkshireWaterSensorEntityDescription(
        key="latest_daily_consumption",
        name="Latest daily consumption",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=_latest_daily_consumption,
        available_fn=_has_daily,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Month-to-date and year-to-date totals are genuine current values
    # (from /your-usage and /yearly-consumption). No state_class: the
    # external statistics are the long-term-stats source of truth, so
    # these stay as plain current-value tiles.
    YorkshireWaterSensorEntityDescription(
        key="consumption_this_month",
        name="Consumption this month",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=_this_month_consumption,
        available_fn=_has_usage,
    ),
    YorkshireWaterSensorEntityDescription(
        key="cost_this_month_clean_water",
        name="Clean water cost this month",
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:currency-gbp",
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=_this_month_clean_cost,
        available_fn=_has_usage,
    ),
    YorkshireWaterSensorEntityDescription(
        key="cost_this_month_sewerage",
        name="Sewerage cost this month",
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:currency-gbp",
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=_this_month_sewerage_cost,
        available_fn=_has_usage,
    ),
    YorkshireWaterSensorEntityDescription(
        key="cost_this_month_total",
        name="Total cost this month",
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:currency-gbp",
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=_this_month_total_cost,
        available_fn=_has_usage,
    ),
    YorkshireWaterSensorEntityDescription(
        key="consumption_year_to_date",
        name="Consumption year to date",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        suggested_display_precision=0,
        value_fn=_ytd_consumption,
        available_fn=_has_yearly,
    ),
    YorkshireWaterSensorEntityDescription(
        key="cost_year_to_date",
        name="Cost year to date",
        device_class=SensorDeviceClass.MONETARY,
        icon="mdi:currency-gbp",
        native_unit_of_measurement="GBP",
        suggested_display_precision=2,
        value_fn=_ytd_total_cost,
        available_fn=_has_yearly,
    ),
    # Date diagnostics (DATE, not midnight-UTC TIMESTAMP):
    #   - "Latest daily reading date": the date of the newest REAL daily
    #     reading we hold (matches Latest daily consumption's
    #     reading_date). This is the honest "newest data" date.
    #   - "YW data refreshed": when YW's systems last rebuilt the summary
    #     (YW's clock, not ours). YW's raw latest_data_date marker, which
    #     runs ~1 day ahead of real data, is exposed only as an attribute
    #     on Latest daily consumption to avoid implying data we lack.
    YorkshireWaterSensorEntityDescription(
        key="last_reading_time",
        translation_key="last_reading_time",
        name="Latest daily reading date",
        device_class=SensorDeviceClass.DATE,
        value_fn=_latest_reading_date,
        available_fn=_has_daily,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    YorkshireWaterSensorEntityDescription(
        key="last_update_time",
        name="YW data refreshed",
        device_class=SensorDeviceClass.DATE,
        value_fn=_yw_data_refreshed,
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
    # Continuous-flow (leak). The API always returns one entry with
    # zeros when there is no leak, so these show a zero baseline and
    # any non-zero value is an early-warning signal.
    YorkshireWaterSensorEntityDescription(
        key="continuous_flow_rate",
        name="Continuous flow rate",
        # A genuine instantaneous measurement (unlike the daily-batch
        # totals), so it keeps state_class=measurement and is tracked in
        # long-term statistics - a slowly rising baseline is an early
        # leak signal worth charting.
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
        icon="mdi:currency-gbp",
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
            entities.extend(
                YorkshireWaterSensor(coordinator, property_data, desc)
                for desc in SENSORS
            )
            entities.append(
                YorkshireWaterMeterStatusSensor(coordinator, property_data),
            )
    # Account-level entities live on the entry-level device, so they
    # exist exactly once and even before any property data has been
    # fetched (e.g. a failed first bootstrap). Health diagnostics plus
    # the account-generic identity (customer name, account number).
    entities.append(YorkshireWaterLastUpdateSensor(coordinator))
    entities.append(YorkshireWaterUpdateStatusSensor(coordinator))
    entities.append(YorkshireWaterCustomerNameSensor(coordinator))
    entities.append(YorkshireWaterAccountNumberSensor(coordinator))
    async_add_entities(entities)


class YorkshireWaterMeterStatusSensor(YorkshireWaterEntity, SensorEntity):
    """Always-available, human-readable meter readiness state.

    Yorkshire Water is rolling smart meters out across the region from
    2025 to 2030, so most accounts go through a `no_meter` and
    `pending_activation` phase before they reach `live`. This sensor is
    always populated so the dashboard makes clear you are waiting on
    Yorkshire Water, not on a broken integration. It also carries the
    property address and account reference as attributes.
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
        """Expose meter status, reference, alarm details, and reading lag.

        The reading date and lag are added only for the latest-reading
        diagnostic sensor.
        """
        snapshot = self.property_data()
        attrs: dict[str, Any] = {}
        if snapshot is None:
            return attrs
        attrs[ATTR_METER_STATUS] = snapshot.meter_status.value
        if snapshot.meter_details:
            attrs[ATTR_METER_REFERENCE] = snapshot.meter_details.meter_reference
        if snapshot.current_consumption is not None:
            attrs[ATTR_ALARM_DETAILS] = [
                alarm.raw
                for alarm in snapshot.current_consumption.continuous_flow_alarm_details
            ]
        # For the "latest daily consumption" diagnostic, surface which
        # day the value is for and how far behind today that is.
        if self.entity_description.key == "latest_daily_consumption":
            point = _latest_point(snapshot)
            if point is not None and point.point_date is not None:
                attrs["reading_date"] = point.point_date.isoformat()
                attrs["cost"] = point.total_cost
                attrs["lag_days"] = (dt_util.now().date() - point.point_date).days
            # YW's own freshness marker, which typically runs ~1 day
            # ahead of the newest published daily total above. Exposed
            # here (not as its own sensor) so it cannot be misread as
            # "the date of our newest reading".
            if (
                snapshot.current_consumption is not None
                and snapshot.current_consumption.latest_data_date is not None
            ):
                attrs["yw_latest_data_date"] = (
                    snapshot.current_consumption.latest_data_date.isoformat()
                )
        attrs[ATTR_LAST_UPDATED] = datetime.now(UTC).isoformat()
        return attrs


# Cap an error string to HA's state-length limit with room to spare.
_MAX_ERROR_LEN = 250


class YorkshireWaterLastUpdateSensor(YorkshireWaterEntryEntity, SensorEntity):
    """When the integration last RAN a poll (succeeded or failed).

    This is the integration's own clock, distinct from the YW-side date
    sensors. It updates on every attempt, so it answers "is the addon
    still running, and when did it last try?". Always available.
    """

    _attr_translation_key = "last_update"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: YorkshireWaterCoordinator) -> None:
        """Bind to the entry-level health device."""
        super().__init__(coordinator, key="last_update")

    @property
    def available(self) -> bool:
        """Always available - it is a health readout, not meter data."""
        return True

    @property
    def native_value(self) -> datetime | None:
        """UTC time of the last poll attempt, or None before the first."""
        return self.coordinator.last_attempt_time


class YorkshireWaterUpdateStatusSensor(YorkshireWaterEntryEntity, SensorEntity):
    """The outcome of the last poll as a stable status enum.

    State is one of `const.UPDATE_STATUSES` (`ok`, `login_failed`,
    `bridge_unreachable`, `api_error`, `unknown_error`, `no_attempt`) -
    low-cardinality so it is history- and automation-friendly. The raw
    short error text is exposed as a `last_error` attribute, never as
    the state. `no_attempt` until the first real poll, so a cache
    restore does not read as `ok`.
    """

    _attr_translation_key = "last_update_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = list(UPDATE_STATUSES)
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: YorkshireWaterCoordinator) -> None:
        """Bind to the entry-level health device."""
        super().__init__(coordinator, key="last_update_status")

    @property
    def available(self) -> bool:
        """Always available so the status survives a failing poll."""
        return True

    @property
    def native_value(self) -> str:
        """The current update-status enum value."""
        return self.coordinator.update_status

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Surface the short error text and the last successful poll time."""
        last_success = self.coordinator.last_success_time
        err = self.coordinator.last_error
        return {
            "last_error": err[:_MAX_ERROR_LEN] if err else None,
            "last_successful_update": (
                last_success.isoformat() if last_success is not None else None
            ),
        }


class YorkshireWaterCustomerNameSensor(YorkshireWaterEntryEntity, SensorEntity):
    """The account holder's name, account-generic so it lives here.

    Contact details (email, phone, title) ride as attributes rather than
    their own sensors, to keep low-value PII out of the state machine
    and recorder while still being visible on the entity.
    """

    _attr_translation_key = "customer_name"
    _attr_icon = "mdi:account"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: YorkshireWaterCoordinator) -> None:
        """Bind to the account device."""
        super().__init__(coordinator, key="customer_name")

    @property
    def native_value(self) -> str | None:
        """The customer's full name, or None before any data."""
        data = self.coordinator.data
        customer = data.customer if data else None
        return customer.full_name if customer else None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Contact details for the account holder."""
        data = self.coordinator.data
        customer = data.customer if data else None
        if customer is None:
            return {}
        attrs: dict[str, str] = {}
        if customer.email:
            attrs["email"] = customer.email
        if customer.mobile_telephone:
            attrs["phone"] = customer.mobile_telephone
        if customer.title:
            attrs["title"] = customer.title
        return attrs


class YorkshireWaterAccountNumberSensor(YorkshireWaterEntryEntity, SensorEntity):
    """The customer / account number, grouped as printed on the bill.

    Account-generic, so it lives on the account device (it used to be
    baked into the config-entry title). With a single property this is
    the customer number; multi-property accounts carry a distinct
    reference per property on each meter device, so this stays blank
    when they differ rather than picking one arbitrarily.
    """

    _attr_translation_key = "account_number"
    _attr_icon = "mdi:identifier"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: YorkshireWaterCoordinator) -> None:
        """Bind to the account device."""
        super().__init__(coordinator, key="account_number")

    @property
    def native_value(self) -> str | None:
        """The bill-grouped account number when unambiguous, else None."""
        data = self.coordinator.data
        if data is None:
            return None
        refs = {
            prop.property.display_account_reference
            for prop in data.properties
            if prop.property.display_account_reference
        }
        if len(refs) == 1:
            return format_account_number(next(iter(refs)))
        return None
