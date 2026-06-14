# Sample dashboards

`yorkshire-water.yaml` is a starter Lovelace dashboard you can paste
into Home Assistant. It assumes the entity IDs the integration creates
by default (one device per property, named after the property's
address; entity IDs include the address slug).

## Installing

1. Open **Settings → Dashboards → Add Dashboard**.
2. Choose **Take control**.
3. In the new dashboard, three-dot menu → **Edit dashboard** → menu
   again → **Raw configuration editor**.
4. Paste the contents of `yorkshire-water.yaml`.
5. **Find-replace the placeholder slug**
   `1_example_street_sometown_anywhere_ex1_1ex` with your
   own property's slug.

## Finding your property's slug

Settings → Devices & Services → Yorkshire Water → click your property
device → click any entity, e.g. *Consumption yesterday*. The entity
shows as `sensor.<slug>_consumption_yesterday`; copy the `<slug>` part.

The slug is derived from the property's address. For an account
registered to *1 Example Street, Sometown, Anywhere, EX1 1EX*
the slug is `1_example_street_sometown_anywhere_ex1_1ex`.

## Multiple properties

This sample is a **single-property** view. The integration itself is
fully multi-property: it creates one device per property on your YW
account, each with its own complete set of entities and its own
backfilled statistics. The sample just happens to point at one
property.

To cover a second (or third) property, **duplicate the whole view**,
not individual cards. In the dashboard's raw configuration, copy the
entire `- title: Smart Meter ...` view block, paste it as a second
view, and in the copy replace **two** identifiers:

1. The **entity-id slug** (`1_example_street_sometown_anywhere_ex1_1ex`)
   everywhere - this keys all the sensor tiles, the binary sensors and
   the button.
2. The **16-digit account reference** (`1234567890123456`) in the
   `yorkshire_water:daily_*` / `yorkshire_water:monthly_*` statistic
   ids - this keys the daily and monthly bar charts. It is your
   property's account number with no spaces (Settings -> Devices &
   Services -> Yorkshire Water -> the property's device).

Then give the copied view its own `title:` (and `path:`) - e.g. the
property's street name - so the two show up as separate tabs.

### Do labels need the property name?

No. Keep the card labels plain ("Today", "This month", "Meter") and
scope each property at the **view** level instead - one tab per
property, titled with the address. Prefixing every tile with the
address (`1 Example Street Today`, `1 Example Street This month` ...)
just adds noise: within a property-titled view the context is already
clear, and each tile's full entity name (shown when you tap it) still
carries the address. If you want the address visible without tapping,
add a single `heading` card with the property address at the top of
each view rather than repeating it on every tile.

## What you get

The view is ordered most-useful first:

- **Status** row: meter active, leak alert, last YW reading date.
- **Consumption** row: yesterday, last 8 days rolling, total tracked.
- **Cost** row: yesterday, total tracked.
- **Monthly** row: consumption and cost for this and last month.
- **Year to date** row: consumption, cost, and monthly averages.
- **Trends** row: monthly consumption and cost bar charts, plus daily
  consumption and cost line charts.
- **Diagnostics** entities list.

The cumulative sensors drive the daily charts and are also suitable as
a source for Home Assistant's built-in Energy Dashboard (under *Water
consumption*).

## Monthly bar charts use external statistics

The two monthly bar charts in the **Trends** row do not read a sensor.
They read external long-term statistics that the integration backfills
from Yorkshire Water's `yearly-consumption` endpoint:

    yorkshire_water:monthly_consumption_<display_account_reference>
    yorkshire_water:monthly_cost_<display_account_reference>

This means the chart shows real monthly totals from the very first
poll, including months from before the integration was installed, with
no waiting for history to accrue.

The `<display_account_reference>` is your property's 16-digit account
number with no spaces. Find it under Settings -> Devices & Services ->
Yorkshire Water -> your property device (it is also the basis of the
entity-id slug). The sample uses the placeholder
`1234567890123456`; replace it with yours.

## "No reading" instead of "Unavailable"

The yesterday consumption and cost sensors render as *Unavailable* on
days Yorkshire Water have not yet delivered a reading (see the main
project README for why). Home Assistant's frontend uses *Unavailable*
as a built-in label that integrations cannot override on
a numeric sensor.

If you want those tiles to say *No reading* instead, install the HACS
frontend `mushroom` add-on and replace each tile card with a
`custom:mushroom-template-card`:

```yaml
type: custom:mushroom-template-card
entity: sensor.<slug>_consumption_yesterday
primary: Yesterday
secondary: >-
  {% set s = states(config.entity) %}
  {% if s in ['unavailable', 'unknown', 'none'] %}
    No reading
  {% else %}
    {{ s | float | round(0) }} L
  {% endif %}
icon: mdi:water
icon_color: blue
fill_container: true
```

For cost sensors swap the unit and precision (`£{{ s | float | round(2) }}`)
and the icon (`mdi:currency-gbp`, `icon_color: green`).
