"""The Yorkshire Water integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.recorder import get_instance
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

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
    MANUFACTURER,
)
from .coordinator import YorkshireWaterCoordinator, YorkshireWaterData
from .statistics import async_remove_statistics_ledger

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
    _drop_deprecated_entry_entities(hass, entry)

    # One-time migration: rename address-derived entity_ids to the
    # account-based scheme so the home address is no longer embedded in
    # entity_ids. Runs before platform setup; the recorder migrates the
    # long-term statistics automatically when the rename event fires.
    _migrate_address_entity_ids(hass, entry, coordinator)

    # One-time cleanup: purge the long-term statistics orphaned by the
    # v3.0.0 statistics-first redesign (dropped sensors, plus summaries
    # that lost their state_class). Runs after the privacy migration so
    # any account-scheme rename has already settled.
    _clear_deprecated_statistics(hass, entry, coordinator)

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

    # Register the account-level hub device up front, before the
    # platforms set up. The per-property meter devices reference it via
    # `via_device`, which only resolves if the hub already exists when
    # those devices are created - otherwise the link is silently dropped
    # and the meters do not nest under the account device.
    _register_account_device(hass, entry)

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
    # Removed in v1.6.3. YW only publish complete daily totals, so a
    # figure for the current (unfinished) day can never exist - these
    # sensors were structurally always unavailable.
    "consumption_today",
    "cost_today",
    # Removed in v3.0.0 (statistics-first redesign). Daily/yesterday and
    # rolling-window values are misleading for daily-batch, ~2-day-lagged
    # data; the dated history now lives in long-term statistics. The
    # cumulative RestoreEntity sensors are replaced by the external
    # daily statistic as the Energy Dashboard source. The last-month and
    # monthly-average values remain available via the monthly statistics.
    "consumption_yesterday",
    "cost_yesterday",
    "window_consumption",
    "cumulative_consumption",
    "cumulative_cost",
    "consumption_last_month",
    "cost_last_month_total",
    "average_monthly_consumption",
    "average_monthly_cost",
)


# Keys of sensors that survive into v3.0.0 but had their `state_class`
# removed (the month-to-date and year-to-date summaries). With the
# statistics-first redesign their dated history lives in the external
# statistics instead, so the live state_class was dropped. HA keeps any
# long-term statistics those sensors recorded under their old
# state_class, then raises a "no longer has a state class" repair and
# leaves the rows orphaned. We clear that stale history once on upgrade.
# `continuous_flow_rate` is deliberately absent: it keeps its
# MEASUREMENT state_class, so its statistics stay valid.
_STAT_CLASS_REMOVED_KEYS: tuple[str, ...] = (
    "consumption_this_month",
    "cost_this_month_clean_water",
    "cost_this_month_sewerage",
    "cost_this_month_total",
    "consumption_year_to_date",
    "cost_year_to_date",
)


_ENTITY_ID_PRIVACY_MIGRATION = "entity_id_privacy_migration_done"
_STATS_CLEANUP_V3 = "stats_cleanup_v3_done"


def _migrate_address_entity_ids(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
    coordinator: YorkshireWaterCoordinator,
) -> None:
    """Rename address-derived entity_ids to the account-based scheme.

    Up to v1.x the device was named after the property address and
    `has_entity_name` prefixed every entity_id with the address slug
    (`sensor.1_example_street_..._meter_status`), embedding the home
    address in entity_ids, logs, the recorder and diagnostics exports.

    From v2.0 `suggested_object_id` keys entity_ids on the account
    reference instead. New installs get that directly; this migration
    renames the entity_ids of existing installs in the registry.

    Safe and idempotent:
    - Keyed off the stable `unique_id` (which is already
      `{account}_{key}`); the target entity_id is `{domain}.{unique_id}`.
    - Only renames entity_ids that still start with the address slug -
      i.e. ones HA auto-generated. An entity_id the user has customised
      to something else is left alone.
    - The recorder migrates each entity's long-term statistics
      automatically in response to the registry rename event, so no
      history is lost.

    Runs once; guarded by a flag in the config entry data. If the
    coordinator has no snapshot yet (first run never reached the API)
    the flag is not set, so it retries on a later setup once data
    exists.
    """
    if entry.data.get(_ENTITY_ID_PRIVACY_MIGRATION):
        return
    snapshot = coordinator.data
    if snapshot is None:
        return  # No property data yet; retry on a future setup.

    ent_reg = er.async_get(hass)
    for prop_data in snapshot.properties:
        prop = prop_data.property
        account = prop.display_account_reference or prop.account_reference
        address = prop.address.formatted() if prop.address else None
        if not account or not address:
            continue
        address_slug = slugify(address)
        for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            uid = reg_entry.unique_id
            if not uid.startswith(f"{account}_"):
                continue
            domain = reg_entry.entity_id.split(".", 1)[0]
            # Only migrate auto-generated, address-derived entity_ids.
            # An entity_id the user has customised away from the
            # address-slug form (or already on the account scheme) is
            # left untouched.
            if not reg_entry.entity_id.startswith(f"{domain}.{address_slug}"):
                continue
            target = ent_reg.async_get_available_entity_id(
                domain, uid, current_entity_id=reg_entry.entity_id,
            )
            if target != reg_entry.entity_id:
                LOGGER.info(
                    "Migrating YW entity_id %s -> %s",
                    reg_entry.entity_id,
                    target,
                )
                ent_reg.async_update_entity(
                    reg_entry.entity_id, new_entity_id=target,
                )

    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, _ENTITY_ID_PRIVACY_MIGRATION: True},
    )


# Entry-level (account device) entities removed in later versions, keyed
# by (platform, key). Their unique_id is `{entry_id}_{key}`. v3.1.1 drops
# the `update_problem` binary sensor: a `problem` whose off-state reads as
# "OK" was confusing next to the Update status enum, which now carries the
# outcome on its own.
_DEPRECATED_ENTRY_ENTITIES: tuple[tuple[str, str], ...] = (
    ("binary_sensor", "update_problem"),
)


def _register_account_device(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
) -> None:
    """Create the account-level hub device the property meters nest under.

    Must run before platform setup so `via_device` on the per-property
    devices resolves. Idempotent: async_get_or_create updates in place.
    The health and refresh entities attach to this same device via their
    matching identifier.
    """
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_account")},
        manufacturer=MANUFACTURER,
        model="Account",
        name="Yorkshire Water",
        entry_type=dr.DeviceEntryType.SERVICE,
        configuration_url="https://my.yorkshirewater.com",
    )


def _drop_deprecated_entry_entities(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
) -> None:
    """Remove orphaned entry-level entities from earlier versions."""
    ent_reg = er.async_get(hass)
    for platform, key in _DEPRECATED_ENTRY_ENTITIES:
        entity_id = ent_reg.async_get_entity_id(
            platform, DOMAIN, f"{entry.entry_id}_{key}",
        )
        if entity_id:
            LOGGER.info("Removing deprecated YW entry-level entity %s", entity_id)
            ent_reg.async_remove(entity_id)


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
        # The Refresh button moved from the per-property device to the
        # account-level device in v3.1.2; drop the old per-property one.
        old_button = ent_reg.async_get_entity_id(
            "button", DOMAIN, f"{display_ref}_refresh_now",
        )
        if old_button:
            LOGGER.info("Removing deprecated YW per-property button %s", old_button)
            ent_reg.async_remove(old_button)


def _clear_deprecated_statistics(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
    coordinator: YorkshireWaterCoordinator,
) -> None:
    """Purge orphaned long-term statistics left by the v3.0.0 redesign.

    Two groups of sensors leave stale recorder statistics behind on an
    upgrade to v3.0.0:

    - sensors dropped entirely (`_DEPRECATED_SENSOR_KEYS`), whose
      entities `_drop_deprecated_entities` has just removed;
    - summaries that survive but lost their `state_class`
      (`_STAT_CLASS_REMOVED_KEYS`).

    Either way HA raises a "no longer has a state class" repair and
    keeps the old rows. Clearing them removes the repair and the dead
    history. New installs never recorded these, so the clear is a
    harmless no-op there.

    Statistics are keyed on the entity_id, which differs across upgrade
    paths: an install already on the v2.0 account scheme recorded under
    `sensor.<account>_<key>`, while a v1.x install that jumps straight to
    v3 recorded the dropped sensors under the old address slug (they are
    removed before the privacy migration can rename them). We clear both
    candidate ids; a clear of a non-existent statistic is a no-op.

    Runs once, guarded by a flag in the entry data. Requires a snapshot
    so the property references are known; retries on a later setup if
    none exists yet. Quietly skips when the recorder is disabled (no
    statistics can exist in that case).
    """
    if entry.data.get(_STATS_CLEANUP_V3):
        return
    snapshot = coordinator.data
    if snapshot is None:
        return  # No property data yet; retry on a future setup.

    try:
        recorder = get_instance(hass)
    except KeyError:
        # Recorder not configured: there are no statistics to clear.
        # Mark done so we do not keep re-checking on every setup.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, _STATS_CLEANUP_V3: True},
        )
        return

    keys = (*_DEPRECATED_SENSOR_KEYS, *_STAT_CLASS_REMOVED_KEYS)
    statistic_ids: list[str] = []
    for prop_data in snapshot.properties:
        prop = prop_data.property
        account = prop.display_account_reference or prop.account_reference
        slugs = {account} if account else set()
        if prop.address:
            slugs.add(slugify(prop.address.formatted()))
        for slug in slugs:
            statistic_ids.extend(f"sensor.{slug}_{key}" for key in keys)

    if statistic_ids:
        LOGGER.info(
            "Clearing %d orphaned YW statistic series after v3.0.0 upgrade",
            len(statistic_ids),
        )
        recorder.async_clear_statistics(statistic_ids)

    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, _STATS_CLEANUP_V3: True},
    )


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
    # Full shutdown (sets the closing flag, cancels the schedule and
    # heartbeat) so a refresh sleeping through its jitter cannot fire on
    # this now-dead coordinator after the entry is reloaded.
    await coordinator.async_shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant,
    entry: YorkshireWaterConfigEntry,
) -> None:
    """Drop cached snapshot, IdP cookies and the statistics ledger on removal."""
    await remove_snapshot(hass, entry.entry_id)
    await remove_auth_cookies(hass, entry.entry_id)
    await async_remove_statistics_ledger(hass, entry.entry_id)


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
