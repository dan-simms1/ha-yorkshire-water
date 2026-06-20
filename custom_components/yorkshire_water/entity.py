"""Shared base entity for Yorkshire Water.

Each entity is bound to a single property. The device identifier is
the property's `display_account_reference` (the human-readable
16-digit account number printed on YW bills) so a customer with
multiple properties gets one device per property and entities never
shuffle between devices on reauth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEVICE_MODEL, DOMAIN, MANUFACTURER


def _platform_domain(entity: object) -> str | None:
    """Return the entity_id platform domain for a YW entity instance.

    Used to suggest an account-based entity_id from the shared base
    class without each platform passing its own domain in.
    """
    if isinstance(entity, BinarySensorEntity):
        return "binary_sensor"
    if isinstance(entity, ButtonEntity):
        return "button"
    if isinstance(entity, SensorEntity):
        return "sensor"
    return None

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
        # Drive the entity_id off the account reference, NOT the device
        # name. With has_entity_name the auto-generated object_id is
        # prefixed with the device name (the property address), baking
        # the home address into every entity_id - and so into logs, the
        # recorder and diagnostics exports. Setting `entity_id` here is
        # the supported way to suggest an un-prefixed object_id at
        # first registration, so new installs get
        # `<platform>.<account>_<key>` while the device keeps its
        # human-friendly address name. Existing installs are migrated
        # to this scheme in __init__.async_setup_entry (the suggestion
        # is ignored once a registry entry already exists).
        domain = _platform_domain(self)
        if domain:
            self.entity_id = f"{domain}.{identifier}_{key}"

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
            # Hang each per-property meter device off the account-level
            # "Yorkshire Water" device (which hosts the health + refresh
            # entities). HA then renders that account device as the
            # parent hub with the smart-meter devices nested beneath it.
            via_device=(DOMAIN, f"{coordinator.entry.entry_id}_account"),
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


class YorkshireWaterEntryEntity(CoordinatorEntity["YorkshireWaterCoordinator"]):
    """Base for integration-level (per config entry) entities.

    Bound to a single service device representing the whole account, not
    a property. Used for the integration-health diagnostics, which are
    account-wide (one poll covers every property) and must exist even
    before any property data has been fetched - e.g. when the first
    bootstrap poll fails. Keying the device on the entry id (not the
    account reference) means these entities never carry the home address
    and are available regardless of fetch state.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: YorkshireWaterCoordinator,
        *,
        key: str,
    ) -> None:
        """Wire the entry-level service device onto the entity."""
        super().__init__(coordinator)
        entry_id = coordinator.entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        # Clean, address-free entity_id. HA disambiguates a second entry
        # with a numeric suffix.
        domain = _platform_domain(self)
        if domain:
            self.entity_id = f"{domain}.yorkshire_water_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_account")},
            manufacturer=MANUFACTURER,
            model="Account",
            name="Yorkshire Water Account",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://my.yorkshirewater.com",
        )
