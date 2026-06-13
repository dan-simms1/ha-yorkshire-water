"""The Yorkshire Water integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .cache import (
    load_auth_cookies,
    load_snapshot,
    remove_auth_cookies,
    remove_snapshot,
)
from .const import (
    BROWSER_ENGINE_PLAYWRIGHT,
    CONF_BROWSER_ENGINE,
    CONF_EMAIL,
    CONF_NODRIVER_URL,
    CONF_PASSWORD,
    CONF_PLAYWRIGHT_URL,
    DEFAULT_NODRIVER_URL,
    DOMAIN,
    LOGGER,
)
from .coordinator import YorkshireWaterCoordinator, YorkshireWaterData

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]

# Older entry data keys we drop on migration. Each entry up through
# v0.5 stored a `selenium_url`; v0.6 stores `playwright_url` instead.
_LEGACY_DATA_KEYS: tuple[str, ...] = (
    "cookies",  # v0.2/v0.3 cookie-paste mode
    "refresh_token",  # v0.1 ROPC mode
    "selenium_url",  # v0.4-v0.5
)

type YorkshireWaterConfigEntry = ConfigEntry[YorkshireWaterData]


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
) -> bool:
    """Migrate older entries to the current schema.

    v0.x → v2: cookie-paste / ROPC era. Drop legacy keys, clear
    unique_id so reauth can re-establish from the email.
    v2 → v3: v0.4-v0.5 stored `selenium_url`; v0.6+ stores
    `playwright_url`. Drop `selenium_url` so the entry's data shape
    matches the new schema and reauth prompts the user for the
    playwright URL.

    Either way the entry will be missing required keys after migration,
    which forces a reauth flow. The user re-enters and we are clean.
    """
    if entry.version < 2:
        cleaned = {
            k: v for k, v in entry.data.items() if k not in _LEGACY_DATA_KEYS
        }
        hass.config_entries.async_update_entry(
            entry,
            data=cleaned,
            unique_id=None,
            version=2,
            minor_version=0,
        )
        LOGGER.info(
            "Migrated entry %s to v2 schema; reauth will be required",
            entry.entry_id,
        )
    if entry.version < 3:
        cleaned = {
            k: v for k, v in entry.data.items() if k not in _LEGACY_DATA_KEYS
        }
        hass.config_entries.async_update_entry(
            entry,
            data=cleaned,
            version=3,
            minor_version=0,
        )
        LOGGER.info(
            "Migrated entry %s to v3 schema (Playwright bridge); "
            "reauth will be required to add the playwright_url field",
            entry.entry_id,
        )
    if entry.version < 4:
        # v4 introduced the nodriver alternative engine. Migrating
        # entries pin to BROWSER_ENGINE_PLAYWRIGHT explicitly rather
        # than DEFAULT_BROWSER_ENGINE: the default has since been
        # flipped to nodriver, but we never want a silent engine
        # change on an existing install. Patchright is what these
        # entries were running before v4; they keep doing so until
        # the user flips it themselves via Options.
        new_data = dict(entry.data)
        new_data.setdefault(CONF_NODRIVER_URL, DEFAULT_NODRIVER_URL)
        new_options = dict(entry.options)
        new_options.setdefault(CONF_BROWSER_ENGINE, BROWSER_ENGINE_PLAYWRIGHT)
        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            options=new_options,
            version=4,
            minor_version=0,
        )
        LOGGER.info(
            "Migrated entry %s to v4 schema (browser engine selector)",
            entry.entry_id,
        )
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
) -> bool:
    """Set up Yorkshire Water from a config entry."""
    if not all(
        entry.data.get(key)
        for key in (CONF_EMAIL, CONF_PASSWORD, CONF_PLAYWRIGHT_URL)
    ):
        # Migrated entry, or one that lost its credentials. Force a
        # reauth flow so the user can supply v0.6 credentials.
        raise ConfigEntryAuthFailed(
            "Yorkshire Water v0.6 requires email, password and "
            "playwright_url. Please reauthenticate.",
        )

    coordinator = YorkshireWaterCoordinator(hass=hass, entry=entry)
    entry.runtime_data = YorkshireWaterData(coordinator=coordinator)

    # Restore the persisted IdP cookie jar (if any) BEFORE the first
    # refresh. With a live jar the first refresh can use silent
    # renewal and avoid burning the reCAPTCHA budget on a real-browser
    # login.
    coordinator.set_initial_cookies(await load_auth_cookies(hass, entry.entry_id))

    # Try to restore the last successful snapshot from local cache. If
    # we have one, sensors show last-known values immediately and we
    # skip the bootstrap login entirely.
    cached = await load_snapshot(hass, entry.entry_id)
    if cached is not None:
        coordinator.async_set_updated_data(cached)
        LOGGER.info(
            "Restored cached YW snapshot for %s; awaiting scheduled refresh",
            entry.title,
        )
    else:
        # No cache: first-ever setup or cache wiped. Try one refresh
        # so entities get registered, but don't fail setup on a bad
        # outcome. If the refresh fails (reCAPTCHA cooldown, network,
        # etc.), the schedule will retry at the next configured time.
        # Sensors stay unavailable until the first successful fetch.
        LOGGER.info(
            "No cached YW snapshot; attempting bootstrap refresh for %s",
            entry.title,
        )
        await coordinator.async_refresh()
        if not coordinator.last_update_success:
            LOGGER.warning(
                "Bootstrap refresh failed for %s (will retry on schedule)",
                entry.title,
            )

    # Drop any orphaned entities from prior versions before HA
    # registers the current set on this setup pass.
    _drop_deprecated_entities(hass, coordinator)

    # Subscribe to the user's chosen daily refresh schedule. This
    # happens whether the bootstrap succeeded or not - if it failed,
    # the next scheduled clock time is the recovery path.
    coordinator.schedule_refreshes()

    # Heartbeat the IdP session between refreshes so the next data
    # poll can use silent renewal rather than the bridge. Cheap
    # `/connect/authorize?prompt=none` call every ~5 min; toggleable
    # via the heartbeat_minutes option (0 = off).
    coordinator.schedule_heartbeat()

    # Update the entry title from the freshly-fetched data. Single-property
    # accounts get a title of the form
    # "Yorkshire Water Smart Meters (Customer: 1234 5678 9012 345 6)".
    # Multi-property accounts drop the customer suffix because each
    # property has its own account number.
    new_title = _compose_entry_title(coordinator.data)
    if new_title and new_title != entry.title:
        hass.config_entries.async_update_entry(entry, title=new_title)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


# Keys of sensors that previous integration versions registered but
# the current version no longer defines. The shells linger in HA's
# entity registry as `unavailable` until we explicitly remove them.
# Unique ids in this integration are `{display_account_reference}_{key}`.
_DEPRECATED_SENSOR_KEYS: tuple[str, ...] = (
    # Originally added in v1.4.0 as a diagnostic. Renamed to
    # account_start_date in v1.5.0 when we realised the underlying
    # API field was the customer's account-open date and not the
    # smart meter's install date.
    "meter_install_date",
    # Replacement added in v1.5.0, then removed entirely in v1.5.3.
    # YW do not expose the actual meter install date anywhere; the
    # account-open date wasn't useful enough to keep on its own.
    "account_start_date",
)


def _drop_deprecated_entities(
    hass: HomeAssistant,
    coordinator: YorkshireWaterCoordinator,
) -> None:
    """Remove orphaned entities left over from earlier versions."""
    snapshot = coordinator.data
    if snapshot is None:
        return
    ent_reg = er.async_get(hass)
    for prop_data in snapshot.properties:
        display_ref = prop_data.property.display_account_reference
        if not display_ref:
            continue
        for key in _DEPRECATED_SENSOR_KEYS:
            unique_id = f"{display_ref}_{key}"
            entity_id = ent_reg.async_get_entity_id(
                "sensor", DOMAIN, unique_id,
            )
            if entity_id:
                LOGGER.info(
                    "Removing deprecated YW entity %s (unique_id=%s)",
                    entity_id,
                    unique_id,
                )
                ent_reg.async_remove(entity_id)


def _format_account_number(raw: str) -> str:
    """Format the 16-digit YW account number with thousands-style spacing.

    Yorkshire Water print the account number on bills in the grouping
    `1234 5678 9012 345 6` (4-4-4-3-1). We mirror that grouping in
    the integration title for visual consistency. Any non-16-digit
    string is returned as-is so unexpected formats degrade gracefully.
    """
    if not raw or not raw.isdigit() or len(raw) != 16:
        return raw or ""
    return f"{raw[0:4]} {raw[4:8]} {raw[8:12]} {raw[12:15]} {raw[15:]}"


def _compose_entry_title(data: object) -> str:
    """Build the integration entry title from the coordinator snapshot."""
    properties = getattr(data, "properties", None) or []
    if len(properties) == 1:
        display_ref = properties[0].property.display_account_reference
        formatted = _format_account_number(display_ref)
        if formatted:
            return f"Yorkshire Water Smart Meters (Customer: {formatted})"
    return "Yorkshire Water Smart Meters"


async def async_unload_entry(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data.coordinator
    coordinator.cancel_scheduled_refreshes()
    coordinator.cancel_heartbeat()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
) -> None:
    """Drop cached snapshot and IdP cookies when the user removes the integration."""
    await remove_snapshot(hass, entry.entry_id)
    await remove_auth_cookies(hass, entry.entry_id)


async def _async_update_listener(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
) -> None:
    """Reload the integration when its options change."""
    LOGGER.debug("Options updated for entry %s, reloading", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: YorkshireWaterConfigEntry,
    device_entry: DeviceEntry,
) -> bool:
    """Allow the user to delete an orphan Yorkshire Water device.

    Used to recover from schema migrations that change the device
    identifier. v0.4 derived the identifier from the entry's
    unique_id (an email hash); v0.5+ uses the property's
    `display_account_reference`. Upgrading from v0.4 leaves the old
    device with no current owner.

    A device is considered "live" if its identifier matches the
    `display_account_reference` of any property in the latest
    coordinator snapshot. Live devices cannot be removed via the UI
    because removing them would orphan entities the integration is
    actively populating. Anything else is fair game.

    If the coordinator has no data yet (first refresh failed, etc.)
    we allow removal so the user can recover from a stuck state.
    """
    data = config_entry.runtime_data.coordinator.data
    if data is None:
        return True

    live_identifiers = {
        snapshot.property.display_account_reference
        for snapshot in data.properties
        if snapshot.property.display_account_reference
    }
    is_live = any(
        ident[0] == DOMAIN and ident[1] in live_identifiers
        for ident in device_entry.identifiers
    )
    if is_live:
        LOGGER.debug(
            "Refusing to remove device %s: matches a live property identifier",
            device_entry.id,
        )
        return False
    LOGGER.info(
        "Removing orphan Yorkshire Water device %s on user request",
        device_entry.id,
    )
    # Removal of the device entry itself is handled by Home Assistant
    # once we return True. We do not need to call dr.async_remove_device.
    _ = dr  # imported for completeness; HA does the actual removal
    return True
