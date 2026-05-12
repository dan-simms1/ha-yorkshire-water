# Yorkshire Water

Brings smart meter consumption data from `my.yorkshirewater.com` into Home
Assistant. Built on the [`pyyorkshirewater`](https://github.com/dan-simms1/pyyorkshirewater)
library.

This integration is unofficial and not affiliated with Yorkshire Water
Services Limited.

## Features

- Recent, today and yesterday consumption sensors in litres.
- Cumulative consumption and cumulative cost sensors for energy-dashboard
  charts.
- Today and yesterday cost sensors in pounds.
- Last reading time, meter reference and meter active diagnostic sensors.
- **Meter status** sensor with a human-readable value (*No meter installed*,
  *Awaiting activation by Yorkshire Water*, or *Live*). Always available so
  it is obvious when the consumption sensors are unavailable because the
  meter has not been commissioned yet, rather than because something is
  broken.
- Continuous flow alarm binary sensor with alarm details exposed as
  attributes.
- Refresh now button on each property device.
- Multi-property accounts get one device per property.

## Setup

Yorkshire Water's portal is fronted by Akamai with bot management enabled
and protects the login form with invisible reCAPTCHA v3. Their OAuth client
disallows refresh tokens and the password grant; the session has a hard
30-minute server-side cap.

The integration's answer is to drive a real Chromium browser through the
login form on every refresh, capture the cookies, fetch the meter data,
then drop the session. To do that you need one of the companion
stealth-browser add-ons:

- [**Patchright Stealth Browser**](https://github.com/dan-simms1/playwright-stealth-addon) (Patchright fork with a patched Chromium binary).
- [**nodriver Stealth Browser**](https://github.com/dan-simms1/nodriver-stealth-addon) (Python; raw CDP, no WebDriver layer).

Install one (or both — they can run side by side). The integration speaks
the same HTTP API to either, so you can switch engines from the integration's
Options without reauth.

The Add Integration flow asks for your Yorkshire Water email, password, and
the URLs of the two add-ons (defaults point at the local install).

## Options

The Configure dialog lets you change:

- **Browser engine** — `playwright` (Patchright) or `nodriver`. Default is
  `nodriver` (works fresh against Yorkshire Water without the manual
  profile-seasoning ritual that Patchright sometimes still needs against
  Akamai-fronted sites).
- **Both add-on URLs** — handy if you run an add-on on a different host or
  port.
- **Refresh time of day** and **Refreshes per day** — clock-time scheduling.
  Each fire jitters by 0–5 minutes.

Default is one refresh per day at midnight. Yorkshire Water's upstream
cadence is daily-ish so polling more often gives no fresher data and chips
away at the reCAPTCHA score budget.
