# Profile seasoning

Seasoning is the (mostly-manual) procedure of populating an addon's
saved profile with high-trust cookies before automated logins start
running against it. The integration does NOT require seasoning. In
fact, on a fresh install you should not season - try the default
flow first, and only season if you find it failing.

## When you would need it

Empirically, on this codebase as of 2026-05-09:

- **nodriver** addon: works fresh against Yorkshire Water. No
  seasoning required. This is the lighter-weight default for new
  installs.
- **patchright** addon: failed fresh against Yorkshire Water with
  reCAPTCHA v2 challenges (the "click all the buses" image grid).
  Seasoning a profile via 10–15 minutes of real human browsing was
  what got it past the v3 score threshold.

If your scheduled refreshes are repeatedly being blocked by reCAPTCHA
v2 image challenges visible in the addon's screenshot artefacts at
`/tmp/runner_fail_*.png`, the profile is likely too "fresh". Season
it.

## What seasoning is

Each addon stores its saved profile (Playwright `storageState` shape:
cookies + localStorage) at `/data/profiles/<name>.json`. By default
the integration tells the addon to use a profile named
`yorkshire_water`. A flow that includes `save_state: true` actions
(or a successful flow end) writes the cookies present in the browser
context to that file.

A "seasoned" profile is one whose cookies came from a real human
browsing session: searching Google, watching a YouTube video, scrolling
news sites with embedded Google ads/analytics. Those cookies (`NID`,
`_GRECAPTCHA`, `AEC`, etc.) accumulate Google-side trust over their
lifetime, and reCAPTCHA's risk score reads them on every evaluation.

## Recipe

1. Enable VNC on the addon you want to season:
   `vnc_enabled: true` and pick a `vnc_password`. Restart the addon.
2. Open noVNC. For the patchright addon: `http://<ha-host>:7901/vnc.html`.
   For nodriver: `http://<ha-host>:7902/vnc.html`. (Both use whatever
   password you set above.)
3. Trigger a long-running flow with periodic `save_state` checkpoints
   and a sentinel-URL exit:

   ```bash
   curl -X POST http://<ha-host>:3001/run-flow \
     -H 'Content-Type: application/json' \
     -d '{
       "profile": "yorkshire_water",
       "context": { "locale": "en-GB", "timezone_id": "Europe/London" },
       "actions": [
         { "goto": "https://www.google.com" },
         { "sleep_ms": 120000 }, { "save_state": true },
         { "sleep_ms": 120000 }, { "save_state": true },
         { "sleep_ms": 120000 }, { "save_state": true },
         { "wait_for_url_contains": "seasoning=done", "timeout_ms": 1800000 }
       ]
     }'
   ```

   (Use port 3002 for the nodriver addon.)

4. Browse manually via noVNC for as long as you want. Do real human
   things on Google properties (search, YouTube, Maps, Gmail) and
   on UK consumer sites that load Google ads/analytics (BBC, Guardian,
   Sky, etc.). Avoid Cloudflare-fronted sites where the addon's
   automation tells will get you blocked.
5. When done, type `https://www.google.com/?seasoning=done` in the
   address bar. The flow's final `wait_for_url_contains` matches and
   the addon writes the seasoned cookies to disk.

## Operational notes

- **Do not visit Yorkshire Water's own sites during seasoning.**
  The whole point is to build cookies on OTHER sites, then use the
  saved profile for the YW login. Visiting YW manually risks burning
  the score and may trigger the Akamai edge to flag the IP.
- **Profiles are addon-local.** The patchright addon's `/data/`
  volume is separate from nodriver's. A profile seasoned in one
  is invisible to the other. To copy:

  ```bash
  ssh root@<ha-host> 'docker exec addon_<patchright-slug> cat /data/profiles/yorkshire_water.json' \
    > /tmp/seed.json
  docker cp /tmp/seed.json addon_<nodriver-slug>:/data/profiles/yorkshire_water.json
  ```

- **Cookies expire.** Most Google trust cookies last weeks to months,
  so a seasoning session usually carries a long way. If your refreshes
  start failing again after a long quiet period, re-season.
