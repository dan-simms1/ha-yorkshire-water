"""Button entities for the Yorkshire Water integration.

A single Refresh button per property: pressing it queues an immediate
coordinator refresh. Useful for watching the login process via the
Playwright add-on's noVNC console without waiting for the next
scheduled clock fire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription

from .entity import YorkshireWaterEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import YorkshireWaterConfigEntry
    from .coordinator import PropertyData, YorkshireWaterCoordinator


REFRESH_NOW = ButtonEntityDescription(
    key="refresh_now",
    translation_key="refresh_now",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register one Refresh button per property snapshot."""
    coordinator = entry.runtime_data.coordinator
    if coordinator.data is None:
        return
    async_add_entities(
        YorkshireWaterRefreshNowButton(coordinator, snapshot)
        for snapshot in coordinator.data.properties
    )


class YorkshireWaterRefreshNowButton(YorkshireWaterEntity, ButtonEntity):
    """Manually request an immediate coordinator refresh."""

    entity_description = REFRESH_NOW

    def __init__(
        self,
        coordinator: YorkshireWaterCoordinator,
        property_data: PropertyData,
    ) -> None:
        """Wire the button into the property's existing device."""
        super().__init__(
            coordinator,
            property_data=property_data,
            key="refresh_now",
        )

    @property
    def available(self) -> bool:
        """Always available - the button is the recovery path itself.

        CoordinatorEntity's default `available` returns
        `coordinator.last_update_success`, which goes False after the
        first failed refresh. That is exactly when the user most needs
        to press this button (e.g. to retry via noVNC after a
        reCAPTCHA challenge), so we override to always-on.
        """
        return True

    async def async_press(self) -> None:
        """Queue a refresh. The coordinator's debouncer dedupes rapid presses."""
        await self.coordinator.async_request_refresh()
