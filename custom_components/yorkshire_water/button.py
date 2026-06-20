"""Button entities for the Yorkshire Water integration.

A single account-level Refresh button: pressing it queues an immediate
coordinator refresh (which covers every property on the account). It
lives on the same entry-level device as the health diagnostics, since a
refresh is an account-wide action, not a per-property one. Useful for
watching the login process via the add-on's noVNC console without
waiting for the next scheduled clock fire, and as the recovery path out
of a stuck/failed state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity

from .entity import YorkshireWaterEntryEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import YorkshireWaterConfigEntry
    from .coordinator import YorkshireWaterCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register the single account-level Refresh button.

    Created unconditionally (not gated on coordinator data) so it is
    present even after a failed first bootstrap - which is exactly when
    the user needs the manual retry path.
    """
    coordinator = entry.runtime_data.coordinator
    async_add_entities([YorkshireWaterRefreshNowButton(coordinator)])


class YorkshireWaterRefreshNowButton(YorkshireWaterEntryEntity, ButtonEntity):
    """Manually request an immediate coordinator refresh."""

    _attr_translation_key = "refresh_now"

    def __init__(self, coordinator: YorkshireWaterCoordinator) -> None:
        """Wire the button into the entry-level account device."""
        super().__init__(coordinator, key="refresh_now")

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
