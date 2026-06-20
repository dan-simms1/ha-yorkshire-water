"""Binary sensors for the Yorkshire Water integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from pyyorkshirewater import MeterStatus

from .const import ATTR_ALARM_DETAILS, STATUS_NO_ATTEMPT, STATUS_OK
from .entity import YorkshireWaterEntity, YorkshireWaterEntryEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import YorkshireWaterConfigEntry
    from .coordinator import PropertyData, YorkshireWaterCoordinator


@dataclass(frozen=True, kw_only=True)
class YorkshireWaterBinarySensorEntityDescription(BinarySensorEntityDescription):
    """BinarySensorEntityDescription with per-property state and availability."""

    is_on_fn: Callable[[PropertyData], bool | None]
    available_fn: Callable[[PropertyData], bool] | None = None


def _alarm_state(data: PropertyData) -> bool | None:
    if data.current_consumption is None:
        return None
    return data.current_consumption.continuous_flow_alarm_state


def _has_consumption(data: PropertyData) -> bool:
    """The alarm sensor is only meaningful when consumption data exists.

    Without it (e.g. while the meter is `no_meter` or
    `pending_activation`) the alarm has no defined state and HA was
    rendering it as `unknown`, which reads as a faulty data pull. We
    mark it `unavailable` instead - the same way the consumption
    sensors handle pre-LIVE meters - so the dashboard reads honestly.
    """
    return data.current_consumption is not None


def _meter_active(data: PropertyData) -> bool | None:
    if data.meter_status is MeterStatus.NO_METER:
        return False
    return data.meter_status is MeterStatus.LIVE


BINARY_SENSORS: tuple[YorkshireWaterBinarySensorEntityDescription, ...] = (
    YorkshireWaterBinarySensorEntityDescription(
        key="continuous_flow_alarm",
        translation_key="continuous_flow_alarm",
        name="Continuous flow alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=_alarm_state,
        available_fn=_has_consumption,
    ),
    YorkshireWaterBinarySensorEntityDescription(
        key="meter_active",
        translation_key="meter_active",
        name="Meter active",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=_meter_active,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register binary sensor entities per property."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    entities: list[BinarySensorEntity] = []
    if data is not None:
        for property_data in data.properties:
            for desc in BINARY_SENSORS:
                entities.append(
                    YorkshireWaterBinarySensor(coordinator, property_data, desc),
                )
    # Account-wide health problem on the entry-level device, created once
    # and present even before any property data exists.
    entities.append(YorkshireWaterUpdateProblemBinarySensor(coordinator))
    async_add_entities(entities)


class YorkshireWaterBinarySensor(YorkshireWaterEntity, BinarySensorEntity):
    """Binary sensor backed by a BinarySensorEntityDescription with is_on_fn."""

    entity_description: YorkshireWaterBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: YorkshireWaterCoordinator,
        property_data: PropertyData,
        description: YorkshireWaterBinarySensorEntityDescription,
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
        """Hide the sensor when its prerequisite data is missing."""
        if not super().available:
            return False
        snapshot = self.property_data()
        if snapshot is None:
            return False
        check = self.entity_description.available_fn
        return check(snapshot) if check else True

    @property
    def is_on(self) -> bool | None:
        """Return True when the alarm is active or the meter is live."""
        snapshot = self.property_data()
        if snapshot is None:
            return None
        return self.entity_description.is_on_fn(snapshot)

    @property
    def extra_state_attributes(self) -> dict[str, list[dict[str, object]]]:
        """Expose the raw alarm details for the continuous flow alarm sensor."""
        if self.entity_description.key != "continuous_flow_alarm":
            return {}
        snapshot = self.property_data()
        if snapshot is None or snapshot.current_consumption is None:
            return {}
        return {
            ATTR_ALARM_DETAILS: [
                alarm.raw
                for alarm in snapshot.current_consumption.continuous_flow_alarm_details
            ],
        }


# Cap the surfaced error string well under HA's attribute expectations.
_MAX_ERROR_LEN = 250


class YorkshireWaterUpdateProblemBinarySensor(
    YorkshireWaterEntryEntity, BinarySensorEntity,
):
    """On when the last poll failed; off when healthy or not yet run.

    Reads the coordinator's OWN status (updated on every attempt,
    including repeated failures HA would otherwise not re-notify), not
    HA's `last_update_success`. Bound to the entry-level device and
    always available, so it stays visible during the very failure it
    reports. Failure reason and timing are exposed as attributes for
    triage and automations.
    """

    _attr_translation_key = "update_problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: YorkshireWaterCoordinator) -> None:
        """Bind to the entry-level health device."""
        super().__init__(coordinator, key="update_problem")

    @property
    def available(self) -> bool:
        """Always available - it must report the failure that hides others."""
        return True

    @property
    def is_on(self) -> bool:
        """True when the last poll attempt failed (ok / no_attempt are off)."""
        return self.coordinator.update_status not in (STATUS_OK, STATUS_NO_ATTEMPT)

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Surface the failure reason and the last success / attempt times."""
        err = self.coordinator.last_error
        last_success = self.coordinator.last_success_time
        last_attempt = self.coordinator.last_attempt_time
        return {
            "status": self.coordinator.update_status,
            "last_error": err[:_MAX_ERROR_LEN] if err else None,
            "last_successful_update": (
                last_success.isoformat() if last_success is not None else None
            ),
            "last_attempt": (
                last_attempt.isoformat() if last_attempt is not None else None
            ),
        }
