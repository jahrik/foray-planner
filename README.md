# Foray Planner

[![CI](https://github.com/jahrik/foray-planner/actions/workflows/ci.yml/badge.svg)](https://github.com/jahrik/foray-planner/actions/workflows/ci.yml)
[![CD](https://github.com/jahrik/foray-planner/actions/workflows/cd.yml/badge.svg)](https://github.com/jahrik/foray-planner/actions/workflows/cd.yml)

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

Each card has three tabs:

- **Species** - clickable chips for each target species active there, linking to its
  iNaturalist page.
- **Calendar** - a 12-month heatmap for that region, loaded on first click - darker
  cells mean more observations historically for that month. Good for planning weeks
  out: "is late October really the right time here, or should I wait until November?"
- **Photos** - thumbnails from the region's most recent observations, loaded on first
  click. Only photos with a redisplayable Creative Commons license show a thumbnail
  (with attribution); everything else still lists with a link back to its iNat page.

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
| **Dispersed** | OSM-tagged free campsites + likely dispersed zones | Teal solid = reported site, Teal dashed ring = proxy |
| **Free only** | Filters both camping layers to free/no-fee options only | - |
| **Public land** | Land ownership polygons shaded by agency (BLM/USFS) | Ochre = BLM, Violet = USFS |
| **Trails** | Hiking paths, named routes & trailheads near the hotspot | Red lines = trails, Red dots = trailheads |

**A note on dispersed camping:** the dashed-ring markers are a *best-guess proxy* -
drivable forest roads that fall on BLM or USFS land, where dispersed camping is
generally allowed. They are not a guarantee of legality. Always check with the local
BLM or Forest Service district office before camping somewhere unfamiliar. The
ownership polygons show who manages the land; they are informational only.

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

## Plan route tab - a multi-stop itinerary

`foray plan` (or `GET /api/plan`, or the **🗺️ Plan route** tab) sequences the top
destinations into a greedy multi-stop itinerary - each stop a region with active
targets and, if required, a nearby free camp - ordered from home to keep the driving
down. The tab plots the route on the map and lists each stop with its drive distance
and nearest camp; **Max stops**, **Max leg (km)**, and **Require free camp** tune it.
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
make install && make db
make ingest             # pull iNat observations for all coverage regions
make start              # http://localhost:8000 (app + postgres)
make scheduler          # optional: background ingest/refresh loop
```

Run `make check` before pushing (lint + type-check + tests). See the
[development guide](docs/development.md) for full details and all Makefile targets.

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
ArcGIS services. Geocoding (c) OpenStreetMap / Nominatim.

---

## License

[MIT](LICENSE)
