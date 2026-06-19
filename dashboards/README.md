# Sample dashboards

`yorkshire-water.yaml` is a starter Lovelace dashboard you can paste
into Home Assistant. Entity IDs and statistic IDs are both keyed on the
property's 16-digit account reference (one device per property), e.g.
`sensor.1234567890123456_latest_daily_consumption`.

## Installing

1. Open **Settings → Dashboards → Add Dashboard**.
2. Choose **Take control**.
3. In the new dashboard, three-dot menu → **Edit dashboard** → menu
   again → **Raw configuration editor**.
4. Paste the contents of `yorkshire-water.yaml`.
5. **Find-replace the placeholder account number** `1234567890123456`
   with your own property's account reference.

## Finding your property's account reference

Settings → Devices & Services → Yorkshire Water → click your property
device → click any entity, e.g. *Latest daily consumption*. The entity
shows as `sensor.<account>_latest_daily_consumption`; copy the
`<account>` part.

This is your 16-digit Yorkshire Water account number with no spaces -
the single value that keys the entity IDs *and* the
`yorkshire_water:*` statistic IDs the charts read. Entity IDs are
deliberately keyed on this rather than the address so the home address
is not embedded in entity IDs, logs or diagnostics exports.

## Multiple properties

This sample is a **single-property** view. The integration is fully
multi-property: one device per property, each with its own entities and
statistics. To add a second, **duplicate the whole `- title: Smart
Meter ...` view block**, give the copy its own `title:`/`path:`, and
find-replace the account number `1234567890123456` with the second
property's. That single number keys everything, so it is the only
identifier to swap. Keep the per-property address at the **view** level
(the heading card already reads it dynamically) rather than prefixing
every tile.

## What you get

Most-useful first:

- **Status** row: meter active, leak alert, last YW reading date.
- **Consumption** row: latest delivered daily reading, month-to-date,
  year-to-date.
- **Cost** row: month-to-date (total, clean water, sewerage) and
  year-to-date.
- **Trends** row: daily and monthly consumption + cost **bar charts**,
  read from long-term statistics.
- **Diagnostics** entities list.

## The charts read long-term statistics, not sensors

Yorkshire Water water meters report ~daily and their per-day breakdown
lands a couple of days late, so the per-day and per-month history is
not exposed as live sensors - it is imported into Home Assistant
long-term statistics, dated to each reading, and the Trends bar charts
read those:

    yorkshire_water:daily_consumption_<account>
    yorkshire_water:daily_cost_<account>
    yorkshire_water:monthly_consumption_<account>
    yorkshire_water:monthly_cost_<account>

This means the charts show real history from the very first poll,
including data from before the integration was installed, with no
waiting for history to accrue. The daily-consumption statistic carries
a monotonic cumulative `sum`, so it is also the recommended **Energy
Dashboard** water source (Settings → Dashboards → Energy → Add water
source), with `yorkshire_water:daily_cost_<account>` as its cost stat.

The tiles, by contrast, show genuinely-current values: the freshest
delivered daily reading (`latest_daily_consumption`, with its
`reading_date` and `lag_days` as attributes), month-to-date and
year-to-date totals, meter status and leak detection.
