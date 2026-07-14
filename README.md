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

The main view. Pick one or more months, hit **Rank destinations**, and the map fills
with hotspot markers ranked by historical fruiting activity for that time of year.
The side panel lists them in order with a score bar, the number of species active
there, and small clickable chips for each one that open the species' iNaturalist page.

- **Magenta markers** = strong historical signal for the selected months
- **Cyan markers** = magenta + something was actually observed there recently
- Bigger marker = stronger signal; marker size and color update every time you re-rank

Clicking a card in the side panel zooms the map to that region and loads its
12-month calendar automatically.

### Place calendar tab - when is the best time for a specific spot?

Click any ranked destination card and the side panel switches to a 12-month heatmap
for that region - darker cells mean more observations historically for that month.
Great for planning a trip weeks out: "is late October really the right time for this
area, or should I wait until November?"

### Fruiting now tab - what's been spotted recently?

Shows only areas where target species were actually observed in the trailing few weeks.
No historical averaging - just what's happening on the ground right now. Each alert
links directly to the iNat observation page and flags obscured (GPS-fuzzy) sightings.

---

## Camping layers

After you rank destinations, click any region card and the camping and trail layers load
for that area. Toggle them on from the **Camping** controls:

| Toggle | What it shows | Marker |
|---|---|---|
| **Show campgrounds** | Named campgrounds from Recreation.gov | Gold = free, Amber = fee/unknown |
| **Show dispersed camping (OSM)** | OSM-tagged free campsites + likely dispersed zones | Teal solid = reported site, Teal dashed ring = proxy |
| **Free only** | Filters both layers to free/no-fee options only | - |
| **Show public land (BLM/USFS)** | Land ownership polygons shaded by agency | Ochre = BLM, Violet = USFS |
| **Show trails (OSM)** | Hiking paths, named routes & trailheads near the hotspot | Red lines = trails, Red dots = trailheads |

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
| **Months** | Toggle any combination of months. The current month is on by default. |
| **Target species** | Narrow to one or more genera (Ctrl/Cmd-click for multiples). Leave unselected to include all 21 targets. |
| **Rank destinations** | Score and plot regions for the selected months + species. |
| **Refresh data** | Re-pulls the latest observations from iNaturalist for the current area. Runs in the background; a status line shows progress. |
| **Coverage indicator** | Shows how fresh each region's data is (e.g. "Washington: 3d ago"). |
| **Theme toggle** | Switch between dark (the default) and light; the map basemap follows. Your choice is remembered across visits. |
| **Units toggle** | Switch between kilometers and miles for distance displays. |

---

## Trip planning (CLI / API)

`foray plan` (or `GET /api/plan`) sequences the top destinations into a greedy multi-stop
itinerary - each stop a region with active targets **and** a nearby free camp, ordered from
home to keep the driving down. A map-based itinerary view is coming; for now see the
[development guide](docs/development.md#cli-reference).

---

## Target species

The app tracks a curated list of 21 genera - morels, chanterelles, king
boletes, hedgehogs, lobster mushrooms, and more. The full list is in
`src/foray/defaults.py` (or override via the `FORAY_SPECIES` env var).
Each species chip in the UI links directly to its iNaturalist page for photos, range maps, and community notes.

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
- [Deployment](docs/deployment.md) - Docker, ECS/RDS + Cloudflare setup, scheduler, refresh patterns

---

## Attribution

Observation data (c) [iNaturalist](https://www.inaturalist.org) contributors (CC-BY-NC).
Camping data (c) [OpenStreetMap](https://www.openstreetmap.org) contributors (ODbL) and
[Recreation.gov](https://recreation.gov) RIDB API. Land boundaries via BLM and USFS
ArcGIS services. Geocoding (c) OpenStreetMap / Nominatim.

---

## License

[MIT](LICENSE)
