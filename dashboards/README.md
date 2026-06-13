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
device → click any entity, e.g. *Consumption today*. The entity
shows as `sensor.<slug>_consumption_today`; copy the `<slug>` part.

The slug is derived from the property's address. For an account
registered to *1 Example Street, Sometown, Anywhere, EX1 1EX*
the slug is `1_example_street_sometown_anywhere_ex1_1ex`.

## Multiple properties

If you have more than one property on your YW account, the
integration creates one device per property. Duplicate the **Smart
Meter** view in the dashboard, paste a second copy, and replace the
slug in the duplicate with your second property's slug.

## What you get

- **Status** row: meter active state, leak alert, last reading time.
- **Consumption** tiles: today, yesterday, last 8 days rolling, total
  tracked.
- **Cost** tiles: today, yesterday, total tracked.
- **Daily consumption / cost** statistics charts (last 30 days), backed
  by the cumulative sensors.
- **Recent activity** history graph for the last week.
- **Diagnostics** entities list.
- **Monthly** row: consumption and cost for this and last month, plus
  two `statistics-graph` bar charts showing the monthly trend. The
  charts fill in over time as the cumulative sensors accumulate
  long-term statistics.

The cumulative sensors drive the statistics charts and are also
suitable as a source for Home Assistant's built-in Energy
Dashboard (under *Water consumption*).

## "No reading" instead of "Unavailable"

The today / yesterday consumption and cost sensors render as
*Unavailable* on days Yorkshire Water have not yet delivered a reading
(see the main project README for why). Home Assistant's frontend uses
*Unavailable* as a built-in label that integrations cannot override on
a numeric sensor.

If you want those tiles to say *No reading* instead, install the HACS
frontend `mushroom` add-on and replace each tile card with a
`custom:mushroom-template-card`:

```yaml
type: custom:mushroom-template-card
entity: sensor.<slug>_consumption_today
primary: Today
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
