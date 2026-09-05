# Foray Planner

[![CI](https://github.com/jahrik/foray-planner/actions/workflows/ci.yml/badge.svg)](https://github.com/jahrik/foray-planner/actions/workflows/ci.yml)
[![CD](https://github.com/jahrik/foray-planner/actions/workflows/cd.yml/badge.svg)](https://github.com/jahrik/foray-planner/actions/workflows/cd.yml)
[![forayplanner.com](https://img.shields.io/badge/forayplanner.com-2e7d32)](https://forayplanner.com)

A trip-planning tool for mushroom hunters. Point it at where you are or want to go,
and it tells you which areas near you are most likely to be fruiting this month
and points you to the closest hiking trail, campsite, BLM & FS land near that area.

> **No identification or edibility claims are made here.** This is a trip-planning
> and mapping tool only. Every species links to its
> [iNaturalist](https://www.inaturalist.org) page for that kind of information.
> Always verify with an expert before eating anything you find.

---

## What it does

Foray Planner pulls real, research-grade observation records from
[iNaturalist](https://www.inaturalist.org) - the world's largest nature-observation
database - and turns years of accumulated field data into three practical views.

### Destinations tab - where should I go this month?

The main view, and the default on load. Toggle one or more months and the map fills
with hotspot markers ranked by historical fruiting activity for that time of year -
ranking updates automatically as you change months, radius, or location. The side
panel lists them in order with a score bar and the number of species active there.

- **Magenta markers** = strong historical signal for the selected months
- **Cyan markers** = magenta + something was actually observed there recently
- Bigger marker = stronger signal; marker size and color update every time you re-rank
- Clicking a card snaps its marker to true size and dims every other circle to an
  outline so the basemap underneath stays readable in dense clusters

Each card title starts as rank + distance and backfills with a notable place name
(national park/forest, protected area, or nearest settlement) once it's looked up.
Below the title: score, species count, recent-observation count, mean ground elevation,
recent/antecedent rainfall, and a fire-proximity badge when a wildfire or recent burn
scar is nearby - each shown only when that data is available for the region.

Each card has five tabs:

- **Species** - clickable chips for each target species active there, linking to its
  iNaturalist page.
- **Calendar** - a 12-month heatmap for that region, loaded on first click - darker
  cells mean more observations historically for that month. Good for planning weeks
  out: "is late October really the right time here, or should I wait until November?"
- **Photos** - thumbnails from the region's most recent observations, loaded on first
  click. Only photos with a redisplayable Creative Commons license show a thumbnail
  (with attribution); everything else still lists with a link back to its iNat page.
- **Trails** - nearby hiking paths, named routes, and trailheads, loaded on first click.
- **Campgrounds** - nearby developed campgrounds from Recreation.gov, loaded on first
  click.

### Fruiting now tab - what's been spotted recently?

Shows only areas where target species were actually observed in the trailing few weeks.
No historical averaging - just what's happening on the ground right now. Each alert
links directly to the iNat observation page and flags obscured (GPS-fuzzy) sightings.

---

## Camping layers

Each layer is off by default; toggle it on and the map plots it for whichever region
is currently focused (click a card, or fly to a stop on the Plan route tab).
**Public land** and **Trails** live in the always-visible Filters row; **Campgrounds**,
**Dispersed**, and **Free only** live under the Plan route tab's Camping controls, but
apply to the map regardless of which tab you're on.

| Toggle | What it shows | Marker |
|---|---|---|
| **Campgrounds** | Named campgrounds from Recreation.gov | Gold = free, Amber = fee/unknown |
| **Dispersed** | Backcountry / dispersed campsites tagged in OpenStreetMap | Teal dot = reported site |
| **Free only** | Filters both camping layers to free/no-fee options only | - |
| **Public land** | Land ownership polygons shaded by agency (BLM/USFS) | Ochre = BLM, Violet = USFS |
| **Trails** | Hiking paths, named routes & trailheads near the hotspot | Red lines = trails, Red dots = trailheads |
| **Fire** | Active wildfire perimeters/points + recent burn scars (NIFC/MTBS) | Red = active fire, Burnt orange = burn scar (dimmer with age) |

**A note on dispersed camping:** the Dispersed layer shows only sites that someone
has explicitly tagged as campable in OpenStreetMap. A tag is not a guarantee of
legality or current access. Always check with the local BLM or Forest Service
district office before camping somewhere unfamiliar. The ownership polygons show who
manages the land; they are informational only.

---

## Controls

| Control | What it does |
|---|---|
| **Location bar** | Type a place name (`Coos Bay, OR`) or raw `lat,lng`. Scores destinations against cached data for that area. |
| **Radius** | Search radius presets (50/150/300/500 km) from the current location. |
| **Months** | Toggle any combination of months. The current month is on by default; ranking updates automatically. |
| **Refresh** | Re-pulls the latest observations from iNaturalist for the current area. Runs in the background; a status line and progress bar show what's happening. |
| **Theme toggle** | Switch between dark (the default) and light; the map basemap follows. Your choice is remembered across visits. |
| **Units toggle** | Switch between kilometers and miles for distance displays. |
| **Text size toggle** | Bumps up font size across the panel and cards for readability. |

---

## Mobile

On narrow screens the map goes full-screen and the side panel becomes a draggable
bottom sheet (collapsed / half / full) - drag the handle or a card tab to expand it,
Back collapses it instead of leaving the page. Desktop keeps the two-column layout.

---

## Plan route tab - a start-to-destination trip

`foray plan` (or `GET /api/plan`, or the **🗺️ Plan route** tab) plans a trip from a
**Start** (defaults to your current location) to a **Destination** - leave the
destination blank and it auto-picks the best reachable region instead. Stops are the
top-scoring regions along the way, each with a nearby free camp (if required) and a
nearby trail, ordered by progress along the route. The tab plots the route on the map
and lists each stop with its drive distance, camp, and trail; **Max stops**, **Max leg
(km)**, and **Require free camp** tune it. Straight-line v1: legs follow the direct
line between stops, not real roads - see [AGENTS.md](AGENTS.md) for why.
See the [development guide](docs/development.md#cli-reference) for the CLI/API form.

---

## Target genera

The app tracks the full Fungi genus catalog from iNaturalist (~6,000 genera) - search for
any genus and add it to your device's target list, or leave the list empty to see everything
nearby. Each species chip in the UI links directly to its iNaturalist page for photos, range
maps, and community notes.

---

## Quick start

```bash
uv tool install rust-just    # one-time: the `just` command runner
just install && just db
just ingest             # pull iNat observations for all coverage regions
just start              # http://localhost:8000 (app + postgres)
just scheduler          # optional: background ingest/refresh loop
```

Run `just check` before pushing (lint + type-check + tests). See the
[development guide](docs/development.md) for full details and all `just` recipes.

---

## Docs

- [Development guide](docs/development.md) - setup, config, CLI, architecture, scoring formula, adding species, testing
- [Data sources](docs/data-sources.md) - iNaturalist, RIDB, OSM/Overpass, ArcGIS BLM/USFS, Nominatim - licenses, rate limits, what's off-limits
- [Deployment](docs/deployment.md) - Docker, Digital Ocean + Ansible + Cloudflare setup, scheduler, refresh patterns

---

## Attribution

Observation data (c) [iNaturalist](https://www.inaturalist.org) contributors (CC-BY-NC).
Observation photos carry their own per-photo license and attribution, shown under each
thumbnail; only Creative Commons-licensed photos are displayed.
Camping data (c) [OpenStreetMap](https://www.openstreetmap.org) contributors (ODbL) and
[Recreation.gov](https://recreation.gov) RIDB API. Land boundaries via BLM and USFS
ArcGIS services. Geocoding (c) OpenStreetMap / Nominatim. Elevation and rainfall via
[Open-Meteo](https://open-meteo.com); wildfire perimeters and burn scars via NIFC/MTBS.

---

## License

[MIT](LICENSE)
