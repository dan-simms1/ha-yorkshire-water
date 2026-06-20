"""Constants for the Yorkshire Water integration."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "yorkshire_water"
LOGGER: Final = logging.getLogger(__package__)

PLATFORMS: Final = ("sensor", "binary_sensor")

# Config entry data keys.
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_PLAYWRIGHT_URL: Final = "playwright_url"
CONF_NODRIVER_URL: Final = "nodriver_url"
CONF_BROWSER_ENGINE: Final = "browser_engine"

# Browser engine identifiers - one per supported addon.
# - "playwright" -> the Patchright addon's flow runner (port 3001)
# - "nodriver"   -> the nodriver addon's flow runner (port 3002)
# The HTTP API both addons expose is identical; only the underlying
# stack differs. Users can switch between the two via the options
# flow without touching credentials.
BROWSER_ENGINE_PLAYWRIGHT: Final = "playwright"
BROWSER_ENGINE_NODRIVER: Final = "nodriver"
BROWSER_ENGINES: Final = (BROWSER_ENGINE_PLAYWRIGHT, BROWSER_ENGINE_NODRIVER)
# nodriver is the recommended default: fresh installs work without
# the manual seasoning ritual that Patchright sometimes still needs
# against Akamai-fronted sites. Existing entries keep whichever
# engine they were configured with; this only changes the new-entry
# default and the migration default for v3-or-older entries.
DEFAULT_BROWSER_ENGINE: Final = BROWSER_ENGINE_NODRIVER

# Options flow keys.
CONF_REFRESH_TIME: Final = "refresh_time"
CONF_REFRESHES_PER_DAY: Final = "refreshes_per_day"
# Heartbeat keeps the IdP session alive between full data refreshes
# by running silent renewal (`/connect/authorize?prompt=none`) every
# `heartbeat_minutes`. YW's IdP idle timeout is around 30 minutes per
# empirical observation on 2026-06-13; a 5-minute heartbeat sits well
# under that with margin for jitter. Set the option to 0 to disable.
CONF_HEARTBEAT_MINUTES: Final = "heartbeat_minutes"
DEFAULT_HEARTBEAT_MINUTES: Final = 5
MIN_HEARTBEAT_MINUTES: Final = 0
MAX_HEARTBEAT_MINUTES: Final = 25  # safely below YW's ~30-min idle

# Refresh schedule defaults.
#
# Yorkshire Water's upstream cadence is not publicly documented; their
# customer portal is updated somewhere between every 4 hours and every
# 24 hours depending on the meter and the day. Once-daily polling at a
# fixed time of day is sensible: it keeps the integration well below
# the reCAPTCHA-score threshold, and the user can pick a time that
# leaves data ready when they want to look at it.
#
# Schedule: the first refresh fires at REFRESH_TIME local time. If the
# user picks more than one refresh per day, the day is divided evenly
# from REFRESH_TIME forward. So `refresh_time=00:00` with
# `refreshes_per_day=4` fires at 00:00, 06:00, 12:00, and 18:00 local.
DEFAULT_REFRESH_TIME: Final = "00:00:00"
DEFAULT_REFRESHES_PER_DAY: Final = 1
MIN_REFRESHES_PER_DAY: Final = 1
MAX_REFRESHES_PER_DAY: Final = 4

# Default URL for the companion Playwright Stealth Browser add-on's
# HTTP login service.
#
# `homeassistant` is a hostname the supervisor publishes inside its
# docker network. From inside the homeassistant container (where this
# integration runs), it resolves to the docker bridge gateway IP,
# which routes to the host. The host has port 3001 published by the
# Playwright add-on, so this default just works regardless of whether
# the add-on was installed locally or via a custom GitHub repository.
#
# The login service exposes site-specific endpoints under /login/<site>,
# e.g. /login/yorkshire-water. The integration appends the site path
# automatically; this default points at the bridge base URL only.
DEFAULT_PLAYWRIGHT_URL: Final = "http://homeassistant:3001/"

# Default URL for the companion nodriver Stealth Browser addon. Same
# resolution rules as the Playwright URL above; nodriver runs on a
# different port (3002) so both addons can be installed side-by-side.
DEFAULT_NODRIVER_URL: Final = "http://homeassistant:3002/"

# Manufacturer label exposed on the device.
MANUFACTURER: Final = "Yorkshire Water"
DEVICE_MODEL: Final = "Smart water meter"

# Attribute keys.
ATTR_METER_REFERENCE: Final = "meter_reference"
ATTR_METER_STATUS: Final = "meter_status"
ATTR_ALARM_DETAILS: Final = "alarm_details"
ATTR_LAST_UPDATED: Final = "last_updated"

# Integration update-status enum (the "Update status" sensor). Fixed,
# low-cardinality options so the state is history- and automation-friendly;
# the raw error text lives in an attribute, never the state.
STATUS_OK: Final = "ok"
STATUS_NO_ATTEMPT: Final = "no_attempt"
STATUS_LOGIN_FAILED: Final = "login_failed"
STATUS_BRIDGE_UNREACHABLE: Final = "bridge_unreachable"
STATUS_API_ERROR: Final = "api_error"
STATUS_UNKNOWN_ERROR: Final = "unknown_error"
UPDATE_STATUSES: Final = (
    STATUS_OK,
    STATUS_NO_ATTEMPT,
    STATUS_LOGIN_FAILED,
    STATUS_BRIDGE_UNREACHABLE,
    STATUS_API_ERROR,
    STATUS_UNKNOWN_ERROR,
)


def format_account_number(raw: str) -> str:
    """Format the 16-digit YW account number with bill-style grouping.

    Yorkshire Water print the account number on bills grouped
    `1234 5678 9012 345 6` (4-4-4-3-1). Any non-16-digit string is
    returned as-is so unexpected formats degrade gracefully.
    """
    if not raw or not raw.isdigit() or len(raw) != 16:
        return raw or ""
    return f"{raw[0:4]} {raw[4:8]} {raw[8:12]} {raw[12:15]} {raw[15:]}"
