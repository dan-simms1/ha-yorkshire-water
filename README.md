# Yorkshire Water for Home Assistant

A Home Assistant integration that surfaces smart meter consumption data from
`my.yorkshirewater.com`.

This integration is unofficial and not affiliated with Yorkshire Water Services
Limited.

## Status

Early alpha. Yorkshire Water is rolling out smart meters across the region
between 2025 and 2030. Most accounts do not yet have a live meter.

The integration always exposes a **Meter status** sensor with one of three
human-readable values:

- *No meter installed*
- *Awaiting activation by Yorkshire Water*
- *Live*

While the meter is anything other than `live`, the consumption and cost
sensors are deliberately *unavailable* (rather than zeroed) because there is
no real data to surface. The Meter status sensor itself stays available
throughout the rollout so the dashboard makes it clear that you are waiting
on Yorkshire Water, not on a broken integration.

## How auth works

Yorkshire Water's portal protects the login form with invisible Google
reCAPTCHA v3 and exposes only authorization-code-with-PKCE OAuth flows. Their
SPA OAuth client is not allowed the password grant, the device flow or
`offline_access`. Their IdP session has a hard absolute lifetime ceiling that
no amount of silent renewal will extend. The portal sits behind Akamai's
edge with bot management enabled.

The integration's answer is two-stage:

1. **Silent renewal first.** Each refresh tries to mint a fresh OAuth bearer
   token from the persisted IdP cookie jar via `/connect/authorize?prompt=none`.
   This works for the whole session-ceiling window without ever touching a
   browser, which means no reCAPTCHA score burn and no Chromium spawned.
2. **Browser-bridge fallback.** Only when the IdP rejects the cookies as
   expired (`error=login_required`) does the integration call the companion
   stealth-browser add-on to drive a real Chromium through the login form,
   capture the fresh cookie jar, and persist it for subsequent silent renewals.

The rotated `idsrv` / `.AspNetCore.Identity.Application` cookies returned
by each silent renewal are persisted to HA storage so the integration
survives restarts without paying for a fresh real-browser login.

You install the integration AND one of the two companion stealth-browser
add-ons. The add-ons expose the same HTTP flow-runner API, so the
integration speaks to either interchangeably; you pick whichever scores
higher against the bot-management stack on your install.

## The two add-on options

| | Patchright | nodriver |
|---|---|---|
| **Repo** | `dan-simms1/playwright-stealth-addon` | `dan-simms1/nodriver-stealth-addon` |
| **Engine** | Patchright (Playwright fork with a binary-patched Chromium) | nodriver (Python; raw CDP, no WebDriver layer) |
| **Default port** | 3001 | 3002 |
| **Watch via noVNC** | port 7901 | port 7902 |
| **Notes** | Older codebase, more polished. Has historically needed [profile seasoning](docs/SEASONING.md) on Yorkshire Water for the v3 score to clear; once seasoned, very reliable. | Newer codebase, simpler stack. Has worked fresh against Yorkshire Water without seasoning. |

You can install both side by side and switch between them in Options without
reauth. **The integration's default is now nodriver** (it has worked fresh
against Yorkshire Water in our testing, without the profile-seasoning ritual
Patchright sometimes still needs). Existing entries upgraded from earlier
versions of the integration keep their previous engine setting; only fresh
installs get the new default.

## Install

### Step 1 — install one (or both) browser add-ons

In Home Assistant:

1. **Settings → Add-ons → Add-on Store**
2. Three-dot menu → **Repositories**
3. Add one or both of:
   - `https://github.com/dan-simms1/playwright-stealth-addon`
   - `https://github.com/dan-simms1/nodriver-stealth-addon`
4. Find the relevant addon in the store and install it. Defaults are fine.
5. Start the add-on.

### Step 2 — install this integration

#### Via HACS

1. Add this repository as a custom HACS integration repository:
   `https://github.com/dan-simms1/ha-yorkshire-water`.
2. Search for Yorkshire Water in HACS and install.
3. Restart Home Assistant.
4. **Settings → Devices and Services → Add Integration** → Yorkshire Water.

#### Manual

Copy `custom_components/yorkshire_water` into your Home Assistant
`custom_components/` directory and restart.

### Step 3 — configure

Provide:

- Yorkshire Water email and password.
- Patchright add-on URL (default `http://homeassistant:3001/`, correct if
  you installed it on the same Home Assistant instance).
- nodriver add-on URL (default `http://homeassistant:3002/`).

Both URLs are required at setup; only the one matching the chosen engine is
used at runtime. The integration runs a quick reachability check on the
chosen engine before creating the entry.

## Options

Settings → Devices and Services → Yorkshire Water → **Configure**:

| Option | Default | Notes |
|---|---|---|
| Browser engine | `nodriver` | Selects which add-on drives the login. Switch any time without reauth. |
| Patchright add-on URL | `http://homeassistant:3001/` | Editable here; no reauth needed for a URL change. |
| nodriver add-on URL | `http://homeassistant:3002/` | Same. |
| Refresh time of day | `00:00:00` | Local time of the first refresh each day. |
| Refreshes per day | `1` | 1, 2, 3 or 4. The day is divided evenly from the refresh time. |

Each scheduled fire jitters by 0–5 minutes so the actual login does not arrive
exactly on the minute (a behavioural fingerprint signal).

The recommended setting is `1` refresh per day at a time that suits you.
Yorkshire Water's upstream cadence is daily-ish, so polling more often gives
no fresher data and chips away at the reCAPTCHA score budget.

## The Refresh now button

Each property device gets a Refresh now button. Press it to trigger an
immediate login. Useful for:

- Testing a config change without waiting for the next scheduled fire.
- Driving a manual login from the noVNC viewer when you want to watch the
  flow happen.
- Manually solving a v2 image challenge if reCAPTCHA throws one at you.

The button stays available even when the last refresh failed, so it is also
the recovery path out of a stuck state.

## Polling

The integration uses clock-time scheduling rather than the
DataUpdateCoordinator's standard interval. Refreshes fire at the configured
local times, not at HA-startup-relative offsets. Restarts do NOT trigger an
extra login: the integration restores the last successful snapshot from
`/config/.storage/yorkshire_water.<entry>.snapshot` on startup and waits for
the next scheduled fire.

## Entities

Once the meter is live, the integration creates a single device per property
with the following entities:

| Entity | Type | Notes |
|---|---|---|
| Recent consumption | sensor | Sum of the rolling window returned by Yorkshire Water, in litres |
| Consumption today | sensor | Daily total in litres |
| Consumption yesterday | sensor | Yesterday's daily total in litres |
| Cumulative consumption | sensor | Running total in litres for energy-dashboard charts |
| Cumulative cost | sensor | Running cost total in pounds |
| Cost today | sensor | Today's cost total in pounds |
| Cost yesterday | sensor | Yesterday's cost total in pounds |
| Last reading time | sensor | Diagnostic; date of the most recent point |
| Meter reference | sensor | Diagnostic; the meter identifier |
| Meter status | sensor | Diagnostic; always available. One of *No meter installed*, *Awaiting activation by Yorkshire Water*, *Live*. |
| Continuous flow alarm | binary sensor | Yorkshire Water's leak alert |
| Meter active | binary sensor | Diagnostic; true once the meter is live |
| Refresh now | button | Manually trigger a coordinator refresh |

Multi-property accounts get one device per property.

## Reauthentication

If your Yorkshire Water password changes, Home Assistant will raise a repair
issue and start a reauth flow. Provide the email and password again to
continue.

Transient login failures (reCAPTCHA cooldown, YW form glitches, addon down)
do NOT trigger reauth. They are recorded as `last_update_success=False` and
retried at the next scheduled fire.

## Troubleshooting

If the scheduled refreshes start failing repeatedly with `Flow runner
rejected: ... (failed at action 19)` style errors, the bot-management score
on the chosen engine has fallen too low. Two things to try:

1. Switch engine. Patchright and nodriver have different fingerprints; one
   often passes when the other does not.
2. Season the profile. See [docs/SEASONING.md](docs/SEASONING.md) for the
   procedure.

If both engines fail, watch a manual login via noVNC (Refresh now button +
the engine's `vnc.html` URL on port 7901 / 7902). Solving the v2 image
challenge by hand once typically gets cookies seeded and subsequent
automated runs work for a while.

## Disclaimer

This project is unofficial and not affiliated with, endorsed by or supported
by Yorkshire Water Services Limited. Trademarks are the property of their
respective owners. Use this software at your own risk and only against
accounts that you own.

## Licence

MIT. See [LICENSE](LICENSE).
