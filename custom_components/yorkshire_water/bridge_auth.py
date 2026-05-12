"""Yorkshire Water login over the generic Playwright flow runner.

The companion `Playwright Stealth Browser` add-on exposes a generic
`POST /run-flow` endpoint that runs a structured action list against
a stealth-masked Chromium and returns the resulting cookie jar. The
add-on knows nothing about Yorkshire Water; this module owns the
selectors and ordering.

If the action vocabulary needs extending (e.g. a new wait condition
or a new way to interact with a form), add the new action to the
add-on's runner first, then use it here.
"""

from __future__ import annotations

from typing import Any

import httpx

from .const import LOGGER

_FLOW_PATH = "/run-flow"
_HEALTH_PATH = "/healthz"
_DEFAULT_TIMEOUT_SECONDS = 120.0
_HEALTH_TIMEOUT_SECONDS = 10.0

_PORTAL_HOST = "my.yorkshirewater.com"
_IDP_HOST = "login.yorkshirewater.com"
_COOKIE_DOMAIN_FILTER = "yorkshirewater.com"

# Profile name passed to the Playwright add-on so the runner can
# persist storageState (cookies + localStorage) across invocations.
# Carrying Google's first-party reCAPTCHA cookies (NID, _GRECAPTCHA)
# forward is the dominant input to the v3 score; presenting as a
# brand-new visitor every refresh tags us as suspicious regardless
# of how good the static fingerprint mask is.
_BRIDGE_PROFILE = "yorkshire_water"


class BridgeLoginError(RuntimeError):
    """Raised when the login flow rejected the request."""


class BridgeUnreachableError(BridgeLoginError):
    """Raised when the flow runner HTTP endpoint is not reachable."""


def _yw_login_actions() -> list[dict[str, Any]]:
    """The fixed sequence of actions that drives the YW login form.

    The form is at https://login.yorkshirewater.com/account/LoginSignup
    and is two-step within one page:
        1. Email field (#Email) + Next button (#formButton2)
        2. Password section (#btnSubmit container) reveals after
           clicking Next, contains the Password field (#Password) and
           the Log in button (#formButton).
    Both buttons have class `g-recaptcha`; the invisible v3 score
    decides whether the password section reveals or whether YW falls
    through to a v2 image challenge we cannot solve.

    Cookie banner is a OneTrust overlay that intermittently blocks
    the form; we click `#onetrust-accept-btn-handler` opportunistically.
    """
    return [
        {"goto": f"https://{_PORTAL_HOST}/"},
        {"wait_for_url_host": _IDP_HOST, "timeout_ms": 30_000},
        # Pause and wander a little before touching anything. reCAPTCHA
        # collects mouse-move and timing telemetry from page-load
        # onwards; a flatline cursor that lands directly on the email
        # field is one of the strongest "this is a bot" signals there
        # is. The exact coordinates do not matter, only that there is
        # movement and that pauses are not perfectly uniform.
        {"sleep_ms_jitter": [1400, 2400]},
        {"mouse_move": {"x": 540, "y": 320, "steps": 30}},
        {"sleep_ms_jitter": [300, 700]},
        {"mouse_move": {"x": 880, "y": 460, "steps": 25}},
        {"click_if_present": "#onetrust-accept-btn-handler", "timeout_ms": 2000},
        {"sleep_ms_jitter": [500, 1100]},
        {"scroll": {"y": 120}},
        {"sleep_ms_jitter": [400, 900]},
        {"scroll": {"y": -80}},
        {"wait_for_selector": "#Email", "state": "visible", "timeout_ms": 25_000},
        {"hover": "#Email"},
        {"sleep_ms_jitter": [350, 800]},
        # Use `type` (pressSequentially) rather than `set_value`. YW's
        # React form ignores programmatic value sets: the DOM value
        # updates but React's internal state stays empty, and clicking
        # Next then silently rejects the form. Real keystrokes work.
        # delay_jitter_ms breaks the otherwise-perfect inter-key cadence.
        {"type": {
            "selector": "#Email",
            "value": "${email}",
            "delay_ms": 90,
            "delay_jitter_ms": 60,
        }},
        {"sleep_ms_jitter": [700, 1500]},
        {"hover": "#formButton2"},
        {"sleep_ms_jitter": [200, 500]},
        {"click": "#formButton2", "timeout_ms": 15_000},
        {"wait_for_selector_visible_via_css": "#btnSubmit", "timeout_ms": 30_000},
        {"hover": "#Password"},
        {"sleep_ms_jitter": [400, 900]},
        {"type": {
            "selector": "#Password",
            "value": "${password}",
            "delay_ms": 90,
            "delay_jitter_ms": 60,
        }},
        {"sleep_ms_jitter": [800, 1600]},
        {"hover": "#formButton"},
        {"sleep_ms_jitter": [200, 500]},
        {"click": "#formButton", "timeout_ms": 15_000},
        {"wait_for_url_host": _PORTAL_HOST, "timeout_ms": 60_000},
        {"wait_for_url_not_contains": "callback", "timeout_ms": 5_000},
        {"get_cookies": {"domain_filter": _COOKIE_DOMAIN_FILTER}},
    ]


def _flow_url(bridge_url: str) -> str:
    base = bridge_url.rstrip("/")
    return f"{base}{_FLOW_PATH}"


def _health_url(bridge_url: str) -> str:
    base = bridge_url.rstrip("/")
    return f"{base}{_HEALTH_PATH}"


async def bridge_healthcheck(
    bridge_url: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Verify the bridge endpoint is reachable without performing a login.

    Used by the config flow / reauth flow to validate that the user
    typed a working URL before the entry is created. The actual login
    happens at the first coordinator refresh; doing a full login here
    too would double every setup's reCAPTCHA exposure.

    Raises:
        BridgeUnreachableError: connection refused, DNS fail, TLS fail,
            or any non-200 response.
    """
    url = _health_url(bridge_url)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=_HEALTH_TIMEOUT_SECONDS)
    try:
        try:
            response = await client.get(url, timeout=_HEALTH_TIMEOUT_SECONDS)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as err:
            raise BridgeUnreachableError(
                f"Flow runner at {url} is not reachable: {err}",
            ) from err
        if response.status_code != 200:
            raise BridgeUnreachableError(
                f"Flow runner returned HTTP {response.status_code} from {url}",
            )
    finally:
        if owns_client:
            await client.aclose()


async def bridge_login(
    bridge_url: str,
    email: str,
    password: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    """POST a YW login flow to the runner and return the cookie jar.

    Pass the HA-shared `httpx.AsyncClient` when calling from a
    coordinator. When None, a short-lived client is created.

    Raises:
        BridgeUnreachableError: connection refused, DNS fail, TLS fail.
        BridgeLoginError: flow runner returned an error, malformed JSON,
            or an empty cookie jar.
    """
    url = _flow_url(bridge_url)
    payload = {
        "actions": _yw_login_actions(),
        "args": {"email": email, "password": password},
        "context": {
            "locale": "en-GB",
            "timezone_id": "Europe/London",
            "viewport": {"width": 1920, "height": 1080},
        },
        # Persist cookies + localStorage between runs (saved by the
        # add-on under /data/profiles on success, reloaded on next
        # invocation). Without this, every refresh looks like a
        # brand-new visitor to YW and to Google's reCAPTCHA fabric.
        "profile": _BRIDGE_PROFILE,
    }

    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS)
    try:
        try:
            response = await client.post(
                url,
                json=payload,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.NetworkError) as err:
            raise BridgeUnreachableError(
                f"Flow runner at {url} is not reachable: {err}",
            ) from err

        try:
            body = response.json()
        except ValueError as err:
            raise BridgeLoginError(
                f"Flow runner returned non-JSON response (HTTP {response.status_code})",
            ) from err

        if response.status_code >= 400:
            error = body.get("error") if isinstance(body, dict) else None
            failed_at = body.get("failed_action_index") if isinstance(body, dict) else None
            detail = error or f"HTTP {response.status_code}"
            if failed_at is not None:
                detail = f"{detail} (failed at action {failed_at})"
            raise BridgeLoginError(f"Flow runner rejected: {detail}")

        cookies = body.get("cookies") if isinstance(body, dict) else None
        if not isinstance(cookies, dict) or not cookies:
            raise BridgeLoginError(
                "Flow runner returned an empty or malformed cookie jar.",
            )

        sanitised: dict[str, str] = {
            name: value
            for name, value in cookies.items()
            if isinstance(name, str) and isinstance(value, str)
        }
        if not sanitised:
            raise BridgeLoginError(
                "Flow runner returned cookies with non-string keys/values.",
            )

        elapsed_ms = body.get("elapsed_ms") if isinstance(body, dict) else None
        if isinstance(elapsed_ms, (int, float)):
            LOGGER.debug("Flow runner login succeeded in %.1fs", float(elapsed_ms) / 1000.0)
        return sanitised
    finally:
        if owns_client:
            await client.aclose()
