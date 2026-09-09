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
database - and turns years of accumulated field data into a single answer-first flow:
*given where you are, the season, and what you're hunting, where should you go?*

### Where should I go this month?

The default on load. Toggle one or more months and the map fills with hotspot markers
ranked by historical fruiting activity for that time of year - the ranking updates
automatically as you change months, radius, sort, or location. The panel leads with a
one-sentence plain-language "why" for the top pick, then the **top three** as full
cards; **"Show N more regions"** opens the rest of the ranked list in place.

Each card: rank + distance (the title backfills with a notable place name - national
park/forest, protected area, or nearest settlement - once it's looked up), the "why"
sentence (top genus + where the season sits for it, recent rain, a nearby-fire note,
and a nearby trailhead / free camp when one is close), a score bar, a stat line
(score, species count, recent-observation count, mean ground elevation, recent
rainfall), genus chips linking to iNaturalist, and two actions: **Details** and
**+ Plan**. The score is phenology-first, then nudged by nearby wildfire, burn
scars, and how reachable the spot is (a close trailhead helps; nowhere to park or
camp within range hurts).

**Markers** read by rank rather than all looking alike:

- **Top 3** - a filled rust circle with a permanent rank numeral
- **Next 7** - a rust ring
- **The rest** - a small dim moss-green dot
- **Green** = target species were seen there in the last few weeks
- **Purple ring** = the region you have selected

Selecting a card (or its marker) snaps that circle to its true real-world footprint,
drops every other circle to a ring so the basemap stays readable, and drops
verified-location observation pins (spore-pink) inside the footprint.

### Details view

**Details** on a card swaps the panel to a dedicated view with four tabs; **"Back to
results"** returns to the list:

- **Calendar** - a 12-month heatmap for that region: darker cells mean more
  observations historically for that month. Good for planning weeks out: "is late
  October really the right time here, or should I wait until November?"
- **Photos** - thumbnails from the region's most recent observations. Only photos with
  a redisplayable Creative Commons license show a thumbnail (with attribution);
  everything else still lists with a link back to its iNat page.
- **Trails** - trailheads near the hotspot, ranked by the trail they lead to (a named
  route, its length, and how many target-genus finds hug the line) rather than raw
  proximity. Selecting one draws the whole named trail on the map.
- **Campgrounds** - nearby developed campgrounds from Recreation.gov, with a parsed
  nightly fee range and whether they're reservable.

### Active now - what's been spotted recently?

The **Sort** pill offers *Best overall* (the scored ranking), *Active now*, and
*Nearest*. **Active now** switches the list to areas where target species were actually
observed in the trailing few weeks - no historical averaging, just what's happening on
the ground right now. Each chip links straight to the iNat observation and flags
obscured (GPS-fuzzy) sightings.

---

## Layers

Every map overlay lives behind one **Layers** pill. Each is off by default; toggle it
on and the map plots it for whichever region is currently focused (click a card, or fly
to a stop on a planned route). Trails aren't here - they live inside a card's **Details
&rarr; Trails** tab.

| Toggle | What it shows | Marker |
|---|---|---|
| **Campgrounds** | Named campgrounds from Recreation.gov | Gold = free, Amber = fee/unknown |
| **Dispersed** | Backcountry / dispersed campsites tagged in OpenStreetMap | Teal dot = reported site |
| **Free only** | Filters both camping layers to free/no-fee options only | - |
| **BLM / USFS / Tribal land** | Land ownership polygons shaded by agency | Ochre = BLM, Violet = USFS, Blue = Tribal land |
| **Fire & burn scars** | Active wildfire perimeters/points + recent burn scars (NIFC/MTBS) | Red = active fire, Burnt orange = burn scar (dimmer with age) |
| **Aerial imagery** | Fills the selected region's footprint with an Esri satellite image + a matching roads/labels overlay | - |

**A note on dispersed camping:** the Dispersed layer shows only sites that someone
has explicitly tagged as campable in OpenStreetMap. A tag is not a guarantee of
legality or current access. Always check with the local BLM or Forest Service
district office before camping somewhere unfamiliar. The ownership polygons show who
manages the land; they are informational only.

---

## Controls

The floating shell over the map has a **search bar** (with a **⋮** menu for units,
theme and text size), a row of **filter pills**, and the results panel.

| Control | What it does |
|---|---|
| **Search bar** | Type a place name (`Coos Bay, OR`) or raw `lat,lng`. Scores destinations against cached data for that area. |
| **Sort** pill | *Best overall* (scored), *Active now* (seen recently), or *Nearest*. |
| **Radius** pill | Search radius presets (50/150/300/500 km) from the current location. |
| **Months** pill | Toggle any combination of months. The current month is on by default; ranking updates automatically. |
| **Genera** pill | Search the ~6,000-genus catalog and pin your targets; empty = everything nearby. |
| **Layers** pill | All the map overlays (camping, land, fire, aerial) in one popover. |
| **Refresh** (⟳ on the map) | Re-pulls the latest observations from iNaturalist for the current area. Runs in the background; a status line and progress bar show what's happening. |
| **⋮ &rarr; Theme** | Switch between dark (the default) and light; the basemap follows - a quiet grey canvas in light, inverted OSM in dark. Remembered across visits. |
| **⋮ &rarr; Units** | Kilometers or miles for every distance, elevation and rainfall figure. |
| **⋮ &rarr; Text size** | Bumps font size across the whole panel for readability. |

---

## Mobile

On narrow screens the map goes full-screen and the panel becomes a draggable bottom
sheet (collapsed / half / full) - drag the handle to expand it, Back collapses it
instead of leaving the page. Desktop keeps the two-column layout with a slide-out dock.

---

## Plan a route - a start-to-destination trip

Tap **+ Plan** on any card to add it to a route shortlist, then **"Plan a route"** on
the bar at the bottom of the panel (`foray plan` and `GET /api/plan` do the same
headless). It plans a trip from a **Start** (defaults to your current location) to a
**Destination** - leave the destination blank and it auto-picks the best reachable
region. Any regions you added with **+ Plan** are threaded in as required stops; the
remaining slots fill with the top-scoring regions along the way, each with a nearby
free camp (if required) and a nearby trail, ordered by progress. The route draws on
the map and each stop lists its drive distance, camp, and trail; **Max stops**, **Max
leg (km)**, and **Require free camp** tune it. Straight-line v1: legs follow the direct
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
Satellite imagery under a selected destination (c) [Esri](https://www.esri.com).

---

## License

[MIT](LICENSE)
