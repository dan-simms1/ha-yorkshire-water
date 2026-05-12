"""Data update coordinator for Yorkshire Water.

Each refresh runs a fresh Playwright-driven login, harvests the
resulting session cookies, mints an access token via pyyorkshirewater,
fetches all the data we surface, then drops the session. We do not
hold a YW session open between refreshes because YW's hard 30-minute
server-side cap would invalidate it anyway.

For multi-property accounts the smart-meter data is fetched per
property using the `account_reference` scoping kwarg in
pyyorkshirewater.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pyyorkshirewater import (
    CookieSessionExpiredError,
    CurrentConsumption,
    Customer,
    MeterDetails,
    MeterStatus,
    Property,
    YorkshireWaterAPIError,
    YorkshireWaterAuthError,
    YorkshireWaterClient,
)

from .bridge_auth import (
    BridgeLoginError,
    BridgeUnreachableError,
    bridge_login,
)
from .cache import save_snapshot
from .const import (
    BROWSER_ENGINE_NODRIVER,
    CONF_BROWSER_ENGINE,
    CONF_EMAIL,
    CONF_NODRIVER_URL,
    CONF_PASSWORD,
    CONF_PLAYWRIGHT_URL,
    CONF_REFRESH_TIME,
    CONF_REFRESHES_PER_DAY,
    DEFAULT_BROWSER_ENGINE,
    DEFAULT_NODRIVER_URL,
    DEFAULT_PLAYWRIGHT_URL,
    DEFAULT_REFRESH_TIME,
    DEFAULT_REFRESHES_PER_DAY,
    DOMAIN,
    LOGGER,
    MAX_REFRESHES_PER_DAY,
    MIN_REFRESHES_PER_DAY,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


# Maximum extra delay added to each scheduled fire, in seconds. Real
# users do not arrive at the login form at exactly H:M:00 every day,
# and arrival-time distribution is itself an input to reCAPTCHA's
# behavioural risk score. We schedule on the minute and then sleep a
# uniform random 0-300 seconds so the actual request hits between the
# nominal time and nominal+5min.
_SCHEDULE_JITTER_SECONDS = 300


@dataclass(slots=True)
class PropertyData:
    """Per-property snapshot returned alongside the customer."""

    property: Property
    meter_status: MeterStatus = MeterStatus.NO_METER
    meter_details: MeterDetails | None = None
    current_consumption: CurrentConsumption | None = None
    usage_periods: list[Any] = field(default_factory=list)
    daily_points: list[Any] = field(default_factory=list)
    yearly_points: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class YorkshireWaterCoordinatorData:
    """Snapshot returned by the coordinator on each refresh."""

    customer: Customer | None = None
    properties: list[PropertyData] = field(default_factory=list)


@dataclass(slots=True)
class YorkshireWaterData:
    """Runtime data attached to the config entry."""

    coordinator: YorkshireWaterCoordinator


class YorkshireWaterCoordinator(DataUpdateCoordinator[YorkshireWaterCoordinatorData]):
    """Refreshes Yorkshire Water data on a clock-time schedule.

    Unlike DataUpdateCoordinator's default `update_interval` behaviour
    (which triggers `interval` after the last refresh, anchored to
    HA startup time), this coordinator schedules its refreshes at
    fixed times of day in the user's local timezone. The day is
    divided evenly from `CONF_REFRESH_TIME` forward by
    `CONF_REFRESHES_PER_DAY`, so a user picking 06:00 with 4 per day
    gets refreshes at 06:00, 12:00, 18:00, 00:00 local time.
    """

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Configure the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            # No automatic interval; we handle scheduling ourselves.
            update_interval=None,
        )
        self.entry = entry
        self._email: str = entry.data[CONF_EMAIL]
        self._password: str = entry.data[CONF_PASSWORD]
        self._unsub_callbacks: list[Callable[[], None]] = []

    @property
    def _bridge_url(self) -> str:
        """Resolve the bridge URL by selecting the engine the user picked.

        Read fresh on every refresh so engine + URL changes via the
        options flow take effect on the very next scheduled fire (or
        button press) without a coordinator restart. Lookup order is
        options -> data -> default; URLs originally land in data at
        create time and may be overridden in options later.
        """
        engine = self.entry.options.get(
            CONF_BROWSER_ENGINE, DEFAULT_BROWSER_ENGINE,
        )
        opts = self.entry.options
        data = self.entry.data
        if engine == BROWSER_ENGINE_NODRIVER:
            return opts.get(
                CONF_NODRIVER_URL,
                data.get(CONF_NODRIVER_URL, DEFAULT_NODRIVER_URL),
            )
        return opts.get(
            CONF_PLAYWRIGHT_URL,
            data.get(CONF_PLAYWRIGHT_URL, DEFAULT_PLAYWRIGHT_URL),
        )

    def schedule_refreshes(self) -> None:
        """Subscribe to time-of-day callbacks based on the current options.

        Cancels any existing subscriptions before re-registering, so it
        is safe to call again whenever the entry options change.
        """
        self.cancel_scheduled_refreshes()

        refresh_time = _parse_refresh_time(
            self.entry.options.get(CONF_REFRESH_TIME, DEFAULT_REFRESH_TIME),
        )
        per_day = max(
            MIN_REFRESHES_PER_DAY,
            min(
                MAX_REFRESHES_PER_DAY,
                int(self.entry.options.get(
                    CONF_REFRESHES_PER_DAY, DEFAULT_REFRESHES_PER_DAY,
                )),
            ),
        )
        offset_hours = 24 // per_day

        scheduled: list[str] = []
        for i in range(per_day):
            hour = (refresh_time.hour + i * offset_hours) % 24
            unsub = async_track_time_change(
                self.hass,
                self._handle_scheduled_refresh,
                hour=hour,
                minute=refresh_time.minute,
                second=0,
            )
            self._unsub_callbacks.append(unsub)
            scheduled.append(f"{hour:02d}:{refresh_time.minute:02d}")
        LOGGER.info(
            "Scheduled YW refreshes (local time): %s",
            ", ".join(scheduled),
        )

    def cancel_scheduled_refreshes(self) -> None:
        """Drop all time-of-day callbacks."""
        for unsub in self._unsub_callbacks:
            unsub()
        self._unsub_callbacks.clear()

    async def _handle_scheduled_refresh(self, _now: datetime) -> None:
        """Time-of-day callback: jitter then trigger a coordinator refresh."""
        delay = random.uniform(0, _SCHEDULE_JITTER_SECONDS)
        LOGGER.debug(
            "Scheduled refresh fired; jittering %.0fs before request",
            delay,
        )
        await asyncio.sleep(delay)
        await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        """Tear down scheduled callbacks before shutting down the coordinator."""
        self.cancel_scheduled_refreshes()
        await super().async_shutdown()

    async def _async_update_data(self) -> YorkshireWaterCoordinatorData:
        """Fresh login, fetch customer + every property, drop the session."""
        # Use Home Assistant's shared httpx client. Creating one inline
        # via `httpx.AsyncClient(...)` would load SSL CAs synchronously
        # in the event loop on first use, which trips the blocking-call
        # detector. The shared client is initialised on HA startup
        # outside the loop.
        http_client = get_async_client(self.hass)
        try:
            cookies = await bridge_login(
                self._bridge_url,
                self._email,
                self._password,
                http_client=http_client,
            )
        except BridgeUnreachableError as err:
            # The Playwright add-on is not running / not reachable.
            # ConfigEntryNotReady so HA retries setup on its own
            # backoff schedule once the add-on comes back.
            raise ConfigEntryNotReady(str(err)) from err
        except BridgeLoginError as err:
            # Treat as a transient failure rather than triggering
            # reauth. Most causes of "login failed" here are
            # transient: reCAPTCHA score cooldown after repeated
            # attempts, YW serving a v2 image challenge, momentary
            # form-render glitches, network blips. The user's
            # credentials are almost certainly still correct, and
            # surfacing reauth on every transient failure is wrong:
            # it interrupts the user with a misleading prompt while
            # the real fix is "wait an hour".
            #
            # The coordinator will retry on its normal interval. The
            # user can still trigger reauth manually from the UI if
            # they have actually changed their YW password.
            raise UpdateFailed(f"YW login failed (will retry): {err}") from err

        client = YorkshireWaterClient(
            cookies=cookies,
            http_client=http_client,
        )
        try:
            await client.login()
            customer = await client.get_customer()
            properties = await client.iter_properties()
            property_snapshots: list[PropertyData] = []
            seen_meter_refs: set[str] = set()
            for prop in properties:
                snapshot = await self._fetch_property_data(client, prop)
                property_snapshots.append(snapshot)

                # Canary: if two different properties report the
                # same meter reference, the YW API is ignoring the
                # account_reference scope and we cannot
                # distinguish per-property data. Log loudly so a
                # multi-property tester can flag it.
                meter_ref = (
                    snapshot.meter_details.meter_reference
                    if snapshot.meter_details
                    else None
                )
                if meter_ref:
                    if meter_ref in seen_meter_refs:
                        LOGGER.warning(
                            "Multiple properties report the same meter "
                            "reference %s; YW may be ignoring "
                            "account_reference scoping. Per-property "
                            "data will be incorrect for these properties.",
                            meter_ref,
                        )
                    else:
                        seen_meter_refs.add(meter_ref)
        except CookieSessionExpiredError as err:
            # Cookies we just harvested were rejected by the API. YW
            # backend race or transient bug, not a credentials issue.
            raise UpdateFailed(f"Cookies expired immediately: {err}") from err
        except YorkshireWaterAuthError as err:
            # Same logic as BridgeLoginError above: do not trigger
            # reauth on transient API auth failures. The cookies just
            # came from a successful login flow; if the API rejects
            # them now it is almost certainly a YW-side transient.
            raise UpdateFailed(f"YW API rejected fresh cookies (will retry): {err}") from err
        except YorkshireWaterAPIError as err:
            raise UpdateFailed(str(err)) from err
        # We do NOT close the shared http_client; HA owns it.

        snapshot = YorkshireWaterCoordinatorData(
            customer=customer,
            properties=property_snapshots,
        )

        # Persist the snapshot so the next HA restart can restore state
        # without performing another YW login. Quietly swallow storage
        # errors; failure to cache is not failure to refresh.
        try:
            await save_snapshot(self.hass, self.entry.entry_id, snapshot)
        except Exception:
            LOGGER.exception("Failed to persist YW snapshot to cache")

        return snapshot

    async def _fetch_property_data(
        self,
        client: YorkshireWaterClient,
        prop: Property,
    ) -> PropertyData:
        """Fetch the smart-meter data for a single property."""
        ref = prop.account_reference or None

        details = await client.get_meter_details(account_reference=ref)
        consumption = await client.get_current_consumption(account_reference=ref)

        meter_status = _derive_meter_status(details, consumption)

        usage: list[Any] = []
        daily: list[Any] = []
        yearly: list[Any] = []
        if meter_status is MeterStatus.LIVE:
            try:
                usage = await client.get_your_usage(account_reference=ref)
                daily = await client.get_daily_consumption(account_reference=ref)
                yearly = await client.get_yearly_consumption(account_reference=ref)
            except YorkshireWaterAPIError as err:
                LOGGER.debug(
                    "Per-property consumption fetch failed for %s: %s",
                    prop.display_account_reference,
                    err,
                )

        return PropertyData(
            property=prop,
            meter_status=meter_status,
            meter_details=details,
            current_consumption=consumption,
            usage_periods=usage,
            daily_points=daily,
            yearly_points=yearly,
        )


def _derive_meter_status(
    details: MeterDetails | None,
    consumption: CurrentConsumption | None,
) -> MeterStatus:
    """Mirror the YorkshireWaterClient.meter_status logic per property."""
    if details is None or not details.meter_reference:
        return MeterStatus.NO_METER
    if consumption and consumption.is_meter_bau:
        return MeterStatus.LIVE
    return MeterStatus.PENDING_ACTIVATION


def _parse_refresh_time(value: str | object) -> time:
    """Coerce a HA TimeSelector value into a `datetime.time`.

    The selector returns `HH:MM:SS` strings; older entries / hand
    edits might supply `HH:MM`. Fall back to midnight on any
    unparseable input rather than failing setup.
    """
    if not isinstance(value, str):
        return time(0, 0)
    try:
        return time.fromisoformat(value)
    except ValueError:
        # Try HH:MM (selector occasionally drops seconds).
        parts = value.split(":")
        try:
            return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        except (ValueError, IndexError):
            return time(0, 0)
