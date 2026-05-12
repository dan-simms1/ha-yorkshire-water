"""Persistence for the coordinator's last-known snapshot.

Restoring snapshot data across HA restarts means the integration does
not have to perform a Yorkshire Water login on every restart just to
populate sensors. Each successful refresh writes the snapshot here;
on next setup we restore from here and wait for the next scheduled
refresh.

Storage shape is the JSON-serialisable `raw` dict from each
pyyorkshirewater model, so reconstruction is just calling `from_api`
again.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from pyyorkshirewater import (
    ContinuousFlowAlarm,
    CurrentConsumption,
    Customer,
    DailyConsumptionPoint,
    MeterDetails,
    MeterStatus,
    Property,
    UsagePeriod,
    YearlyConsumptionPoint,
)

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import PropertyData, YorkshireWaterCoordinatorData

_STORE_VERSION = 1


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, _STORE_VERSION, f"{DOMAIN}.{entry_id}.snapshot")


async def load_snapshot(
    hass: HomeAssistant,
    entry_id: str,
) -> YorkshireWaterCoordinatorData | None:
    """Restore the most recent snapshot for an entry, if any."""
    from .coordinator import PropertyData, YorkshireWaterCoordinatorData

    raw = await _store(hass, entry_id).async_load()
    if not raw or not isinstance(raw, dict):
        return None
    try:
        customer_raw = raw.get("customer")
        customer = Customer.from_api(customer_raw) if customer_raw else None
        snapshots: list[PropertyData] = []
        for ps in raw.get("properties", []) or []:
            prop_raw = ps.get("property") or {}
            details_raw = ps.get("meter_details")
            consumption_raw = ps.get("current_consumption")
            snapshots.append(
                PropertyData(
                    property=Property.from_api(prop_raw),
                    meter_status=MeterStatus(
                        ps.get("meter_status", MeterStatus.NO_METER.value),
                    ),
                    meter_details=(
                        MeterDetails.from_api(details_raw) if details_raw else None
                    ),
                    current_consumption=(
                        _restore_consumption(consumption_raw)
                        if consumption_raw
                        else None
                    ),
                    usage_periods=[
                        UsagePeriod.from_api(p)
                        for p in ps.get("usage_periods", []) or []
                    ],
                    daily_points=[
                        DailyConsumptionPoint.from_api(p)
                        for p in ps.get("daily_points", []) or []
                    ],
                    yearly_points=[
                        YearlyConsumptionPoint.from_api(p)
                        for p in ps.get("yearly_points", []) or []
                    ],
                ),
            )
        return YorkshireWaterCoordinatorData(
            customer=customer,
            properties=snapshots,
        )
    except (ValueError, KeyError, TypeError) as err:
        LOGGER.warning(
            "Could not restore snapshot for %s: %s; will perform fresh refresh",
            entry_id,
            err,
        )
        return None


async def save_snapshot(
    hass: HomeAssistant,
    entry_id: str,
    data: YorkshireWaterCoordinatorData,
) -> None:
    """Persist the latest coordinator snapshot."""
    payload: dict[str, Any] = {
        "saved_at": datetime.now(UTC).isoformat(),
        "customer": data.customer.raw if data.customer else None,
        "properties": [
            {
                "property": ps.property.raw,
                "meter_status": ps.meter_status.value,
                "meter_details": (
                    ps.meter_details.raw if ps.meter_details else None
                ),
                "current_consumption": (
                    ps.current_consumption.raw if ps.current_consumption else None
                ),
                "usage_periods": [u.raw for u in ps.usage_periods],
                "daily_points": [p.raw for p in ps.daily_points],
                "yearly_points": [p.raw for p in ps.yearly_points],
            }
            for ps in data.properties
        ],
    }
    await _store(hass, entry_id).async_save(payload)


async def remove_snapshot(hass: HomeAssistant, entry_id: str) -> None:
    """Drop any persisted snapshot. Called when an entry is removed."""
    await _store(hass, entry_id).async_remove()


def _restore_consumption(payload: dict[str, Any]) -> CurrentConsumption:
    """`CurrentConsumption.from_api` re-builds nested alarms from the raw."""
    consumption = CurrentConsumption.from_api(payload)
    # Defensive: if the alarm details list went missing in serialisation
    # (it shouldn't), reconstruct empty.
    if not isinstance(consumption.continuous_flow_alarm_details, list):
        consumption.continuous_flow_alarm_details = []
    else:
        consumption.continuous_flow_alarm_details = [
            alarm if isinstance(alarm, ContinuousFlowAlarm)
            else ContinuousFlowAlarm.from_api(alarm)
            for alarm in consumption.continuous_flow_alarm_details
        ]
    return consumption
