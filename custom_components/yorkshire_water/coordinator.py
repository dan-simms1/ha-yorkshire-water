"""Data update coordinator for Yorkshire Water.

Each refresh tries to mint a fresh access token from the stored IdP
cookie jar first, using `pyyorkshirewater`'s built-in silent renewal
(`/connect/authorize?prompt=none`). Only when those cookies have
hit their absolute session ceiling and `CookieSessionExpiredError`
is raised do we fall back to the stealth-browser bridge for a fresh
real-browser login.

This shape exists because YW gates the login form behind reCAPTCHA
v3 — every browser-bridge call costs reCAPTCHA score budget. Doing
the cheap silent renewal first, and only paying for a real-browser
login when the IdP actually demands one, keeps the score budget
intact and makes per-restart polling near-free.

The rotated `idsrv` / `.AspNetCore.Identity.Application` cookies
that the IdP hands back on each silent renewal are persisted via
`save_auth_cookies` so the next poll (or HA restart) starts with
fresh state.

For multi-property accounts the smart-meter data is fetched per
property using the `account_reference` scoping kwarg in
pyyorkshirewater.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
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
    UsagePeriod,
    YearlyConsumption,
    YorkshireWaterAPIError,
    YorkshireWaterAuthError,
    YorkshireWaterClient,
)

from .bridge_auth import (
    BridgeLoginError,
    BridgeUnreachableError,
    bridge_login,
)
from .cache import (
    remove_auth_cookies,
    save_auth_cookies,
    save_snapshot,
)
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
    usage_periods: list[UsagePeriod] = field(default_factory=list)
    daily_points: list[Any] = field(default_factory=list)
    yearly_consumption: YearlyConsumption | None = None


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
        # IdP cookie jar carried across polls. Populated by
        # `__init__.async_setup_entry` from disk before the first
        # refresh; refreshed in-place from `client.cookies` after
        # every successful silent renewal (the library absorbs IdP
        # Set-Cookie rotations during `/connect/authorize`).
        self._cookies: dict[str, str] | None = None
        # Refresh-Now button, scheduled poll, and bootstrap refresh
        # can overlap. Serialise the auth + persist step so the
        # rotated cookie jar is not raced.
        self._auth_lock = asyncio.Lock()

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

    def set_initial_cookies(self, cookies: dict[str, str] | None) -> None:
        """Seed the cookie jar from persistent storage at setup.

        Called once by `__init__.async_setup_entry` after the entry's
        last known cookies are restored from the auth cache. After
        the first successful refresh the jar is maintained in-place
        from `YorkshireWaterClient.cookies`.
        """
        self._cookies = cookies

    async def _async_update_data(self) -> YorkshireWaterCoordinatorData:
        """Refresh data, preferring silent renewal over a browser login.

        Flow:
        1. If we have a stored cookie jar, try silent renewal via the
           library and fetch the snapshot. On `CookieSessionExpiredError`
           (the IdP said `error=login_required`) drop the jar and fall
           through to step 2. Any other error propagates as
           `UpdateFailed` — we do NOT invalidate cookies on transients.
        2. Call the browser bridge for a fresh real-browser login,
           reseed the jar from the returned cookies, fetch again.

        The auth lock around the whole thing prevents the Refresh
        Now button, scheduled polls, and bootstrap refresh from
        racing the rotated-cookie persist step.
        """
        # Use Home Assistant's shared httpx client. Creating one inline
        # via `httpx.AsyncClient(...)` would load SSL CAs synchronously
        # in the event loop on first use, which trips the blocking-call
        # detector. The shared client is initialised on HA startup
        # outside the loop.
        http_client = get_async_client(self.hass)

        async with self._auth_lock:
            if self._cookies:
                try:
                    return await self._fetch_all(
                        http_client,
                        self._cookies,
                        source="stored cookies (silent renewal)",
                    )
                except CookieSessionExpiredError as err:
                    LOGGER.info(
                        "Stored YW IdP cookies expired (%s); "
                        "falling back to browser bridge",
                        err,
                    )
                    self._cookies = None
                    try:
                        await remove_auth_cookies(
                            self.hass, self.entry.entry_id,
                        )
                    except Exception:
                        LOGGER.exception(
                            "Failed to drop expired YW auth cookies from cache",
                        )

            try:
                bridge_cookies = await bridge_login(
                    self._bridge_url,
                    self._email,
                    self._password,
                    http_client=http_client,
                )
            except BridgeUnreachableError as err:
                # The stealth-browser add-on is not running / not
                # reachable. ConfigEntryNotReady so HA retries setup
                # on its own backoff schedule once the add-on comes
                # back.
                raise ConfigEntryNotReady(str(err)) from err
            except BridgeLoginError as err:
                # Treat as a transient failure rather than triggering
                # reauth. Most causes of "login failed" here are
                # transient: reCAPTCHA score cooldown after repeated
                # attempts, YW serving a v2 image challenge, momentary
                # form-render glitches, network blips. The user's
                # credentials are almost certainly still correct, and
                # surfacing reauth on every transient failure is wrong.
                raise UpdateFailed(
                    f"YW login failed (will retry): {err}",
                ) from err

            try:
                return await self._fetch_all(
                    http_client,
                    bridge_cookies,
                    source="fresh browser-bridge login",
                )
            except CookieSessionExpiredError as err:
                # Cookies we just harvested were rejected by the API
                # before we could even use them. YW backend race or
                # transient, not a credentials issue.
                raise UpdateFailed(
                    f"Cookies expired immediately: {err}",
                ) from err

    async def _fetch_all(
        self,
        http_client: Any,
        cookies: dict[str, str],
        *,
        source: str,
    ) -> YorkshireWaterCoordinatorData:
        """Run one refresh against a known cookie jar.

        Performs the silent-renewal handshake, fetches the customer
        plus every property's smart-meter data, persists the rotated
        cookie jar AND the snapshot on success, and returns the
        snapshot. Raises `CookieSessionExpiredError` if the jar is
        dead so the caller can decide whether to drop down to the
        browser bridge.
        """
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
                # account_reference scope and we cannot distinguish
                # per-property data. Log loudly so a multi-property
                # tester can flag it.
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
        except CookieSessionExpiredError:
            raise
        except YorkshireWaterAuthError as err:
            # Non-CookieSessionExpired auth failure: do not invalidate
            # the cookie jar (it just produced a valid bearer; the API
            # failure is almost certainly a YW transient).
            raise UpdateFailed(
                f"YW API rejected fresh cookies (will retry): {err}",
            ) from err
        except YorkshireWaterAPIError as err:
            raise UpdateFailed(str(err)) from err
        # We do NOT close the shared http_client; HA owns it.

        # Persist the rotated cookie jar. After silent renewal the
        # IdP rotates `idsrv.session` and `.AspNetCore.Identity.Application`
        # in-place; not capturing the new values would force a
        # bridge fallback on the very next poll.
        self._cookies = client.cookies
        try:
            await save_auth_cookies(
                self.hass, self.entry.entry_id, client.cookies,
            )
        except Exception:
            LOGGER.exception("Failed to persist YW auth cookies")

        snapshot = YorkshireWaterCoordinatorData(
            customer=customer,
            properties=property_snapshots,
        )

        # Persist the snapshot so the next HA restart can restore
        # state without performing another YW login. Quietly swallow
        # storage errors; failure to cache is not failure to refresh.
        try:
            await save_snapshot(self.hass, self.entry.entry_id, snapshot)
        except Exception:
            LOGGER.exception("Failed to persist YW snapshot to cache")

        LOGGER.debug("YW refresh succeeded via %s", source)
        return snapshot

    async def _fetch_property_data(
        self,
        client: YorkshireWaterClient,
        prop: Property,
    ) -> PropertyData:
        """Fetch the smart-meter data for a single property."""
        # YW has two different shapes of "account reference" depending
        # on the endpoint:
        #   - /account/properties/detail takes the long opaque
        #     `account_reference` token.
        #   - /account/smartmeter/* takes the 15-digit form, which is
        #     `displayAccountReference` minus the trailing check digit.
        # The Property model surfaces both; we pass the 15-digit form
        # here because the smart-meter endpoints reject the opaque
        # token shape with HTTP 404, which the library translates into
        # an empty MeterDetails and ultimately a `no_meter` status.
        display_ref = prop.display_account_reference or ""
        smart_meter_ref = display_ref[:-1] if len(display_ref) >= 15 else None

        details = await client.get_meter_details(account_reference=smart_meter_ref)
        # Smart-meter consumption endpoints take meterReference (the
        # 10-digit ref returned in meter-details), not the long opaque
        # account_reference. Pass it through explicitly so multi-property
        # scoping still works: each property's meter_reference is
        # resolved from that property's own meter-details fetch above.
        meter_ref = details.meter_reference or None
        consumption = await client.get_current_consumption(meter_reference=meter_ref)

        meter_status = _derive_meter_status(details, consumption)

        usage: list[UsagePeriod] = []
        daily: list[Any] = []
        yearly: YearlyConsumption | None = None
        if meter_status is MeterStatus.LIVE:
            # Fetch each endpoint independently so a failure in one (most
            # likely /daily-consumption, which still rejects every param
            # shape we have tried as "Invalid date range") does not drop
            # the others.
            try:
                usage = await client.get_your_usage(meter_reference=meter_ref)
            except YorkshireWaterAPIError as err:
                LOGGER.debug("your-usage fetch failed for %s: %s",
                             prop.display_account_reference, err)
            try:
                # YW requires a moveInDate (the customer's account
                # start date at the property, surfaced by the API as
                # meter-details.startDate) and moveOutDate (today for
                # active customers). pyyorkshirewater fills both in
                # from the client's cached meter-details and today's
                # date when we omit them; we just need a sensible
                # window. 8 days back gives today, yesterday, and a
                # week-back rolling average without burdening the
                # API.
                end = datetime.now().date()
                start = end - timedelta(days=8)
                daily = await client.get_daily_consumption(
                    start_date=start.isoformat(),
                    end_date=end.isoformat(),
                    meter_reference=meter_ref,
                )
            except YorkshireWaterAPIError as err:
                LOGGER.debug("daily-consumption fetch failed for %s: %s",
                             prop.display_account_reference, err)
            try:
                # The yearly endpoint needs `year` as a query param.
                # Use the current-consumption's latest_data_date if we
                # have one, otherwise fall back to the calendar year of
                # the meter-details `currentDate`.
                year = _resolve_consumption_year(details, consumption)
                yearly = await client.get_yearly_consumption(
                    year=year, meter_reference=meter_ref,
                )
            except YorkshireWaterAPIError as err:
                LOGGER.debug("yearly-consumption fetch failed for %s: %s",
                             prop.display_account_reference, err)

        return PropertyData(
            property=prop,
            meter_status=meter_status,
            meter_details=details,
            current_consumption=consumption,
            usage_periods=usage,
            daily_points=daily,
            yearly_consumption=yearly,
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


def _resolve_consumption_year(
    details: MeterDetails | None,
    consumption: CurrentConsumption | None,
) -> int:
    """Pick the year to ask /yearly-consumption about.

    Prefers the year of the latest reading the meter has actually
    produced, since asking about a year with no data returns an empty
    response. Falls back to the meter-details `currentDate`, then to
    the HA host's current year.
    """
    if consumption and consumption.latest_data_date:
        return consumption.latest_data_date.year
    if details and details.current_date:
        return details.current_date.year
    return datetime.now().year


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
