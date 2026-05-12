"""Shared base entity for Yorkshire Water.

Each entity is bound to a single property. The device identifier is
the property's `display_account_reference` (the human-readable
16-digit account number printed on YW bills) so a customer with
multiple properties gets one device per property and entities never
shuffle between devices on reauth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEVICE_MODEL, DOMAIN, MANUFACTURER

if TYPE_CHECKING:
    from .coordinator import (
        PropertyData,
        YorkshireWaterCoordinator,
        YorkshireWaterCoordinatorData,
    )


class YorkshireWaterEntity(CoordinatorEntity["YorkshireWaterCoordinator"]):
    """Base class with shared device info bound to a single property."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: YorkshireWaterCoordinator,
        *,
        property_data: PropertyData,
        key: str,
    ) -> None:
        """Wire the property's device info onto the entity."""
        super().__init__(coordinator)
        prop = property_data.property
        # The integer-like 16-digit display reference is stable across
        # reauth and migration; ideal for both unique_id and device id.
        identifier = prop.display_account_reference or prop.account_reference
        self._account_reference = prop.account_reference
        self._display_account_reference = prop.display_account_reference
        self._attr_unique_id = f"{identifier}_{key}"

        meter_reference = (
            property_data.meter_details.meter_reference
            if property_data.meter_details
            else None
        )

        device_name = prop.address.formatted() or "Yorkshire Water property"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            manufacturer=MANUFACTURER,
            model=DEVICE_MODEL,
            name=device_name,
            serial_number=meter_reference,
            configuration_url="https://my.yorkshirewater.com",
        )

    def property_data(self) -> PropertyData | None:
        """Return the latest snapshot for this property, or None."""
        data: YorkshireWaterCoordinatorData | None = self.coordinator.data
        if data is None:
            return None
        for snapshot in data.properties:
            if (
                snapshot.property.display_account_reference
                == self._display_account_reference
            ):
                return snapshot
        return None
