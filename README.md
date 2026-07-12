# 🍄 Foray Planner

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
[iNaturalist](https://www.inaturalist.org) — the world's largest nature-observation
database — and turns years of accumulated field data into three practical views.

### 🗺️ Destinations tab — where should I go this month?

The main view. Pick one or more months, hit **Rank destinations**, and the map fills
with hotspot markers ranked by historical fruiting activity for that time of year.
The side panel lists them in order with a score bar, the number of species active
there, and small clickable chips for each one that open the species' iNaturalist page.

- **Magenta markers** = strong historical signal for the selected months
- **Cyan markers** = magenta + something was actually observed there recently
- Bigger marker = stronger signal; marker size and color update every time you re-rank

Clicking a card in the side panel zooms the map to that region and loads its
12-month calendar automatically.

### 📅 Place calendar tab — when is the best time for a specific spot?

Click any ranked destination card and the side panel switches to a 12-month heatmap
for that region — darker cells mean more observations historically for that month.
Great for planning a trip weeks out: "is late October really the right time for this
area, or should I wait until November?"

### ⚡ Fruiting now tab — what's been spotted recently?

Shows only areas where target species were actually observed in the trailing few weeks.
No historical averaging — just what's happening on the ground right now. Useful when
you're ready to leave today and want the freshest signal.

---

## Camping layers

After you rank destinations, click any region card and the camping layers load for
that area. Toggle them on from the **Camping** controls:

| Toggle | What it shows | Marker |
|---|---|---|
| **Show campgrounds** | Named campgrounds from Recreation.gov | Gold = free · Amber = fee/unknown |
| **Show dispersed camping (OSM)** | OSM-tagged free campsites + likely dispersed zones | Teal solid = reported site · Teal dashed ring = proxy |
| **Free only** | Filters both layers to free/no-fee options only | — |
| **Show public land (BLM/USFS)** | Land ownership polygons shaded by agency | Brown = BLM · Violet = USFS |

**A note on dispersed camping:** the dashed-ring markers are a *best-guess proxy* —
drivable forest roads that fall on BLM or USFS land, where dispersed camping is
generally allowed. They are not a guarantee of legality. Always check with the local
BLM or Forest Service district office before camping somewhere unfamiliar. The
ownership polygons show who manages the land; they are informational only.

---

## Controls

| Control | What it does |
|---|---|
| **Location bar** | Type a place name (`Coos Bay, OR`) or raw `lat,lng`. The app geocodes it and fetches fresh iNaturalist data for that area. |
| **Months** | Toggle any combination of months. The current month is on by default. |
| **Target species** | Narrow to one or more genera (Ctrl/Cmd-click for multiples). Leave unselected to include all 21 targets. |
| **Rank destinations** | Score and plot regions for the selected months + species. |
| **Refresh data** | Re-pulls the latest observations from iNaturalist for the current area. Runs in the background; a status line shows progress. |

---

## Target species

The app tracks a curated list of 21 genera — morels, chanterelles, king
boletes, hedgehogs, lobster mushrooms, and more. The full list is in
[`data/species_seed.yaml`](data/species_seed.yaml). Each species chip in the UI links
directly to its iNaturalist page for photos, range maps, and community notes.

---

## Docs

- [Development guide](docs/development.md) — setup, config, CLI, architecture, scoring formula, adding species, testing
- [Data sources](docs/data-sources.md) — iNaturalist, RIDB, OSM/Overpass, ArcGIS BLM/USFS, Nominatim — licenses, rate limits, what's off-limits
- [Deployment](docs/deployment.md) — Docker, planned Lightsail + Cloudflare setup, refresh patterns

---

## Attribution

Observation data © [iNaturalist](https://www.inaturalist.org) contributors (CC-BY-NC).
Camping data © [OpenStreetMap](https://www.openstreetmap.org) contributors (ODbL) and
[Recreation.gov](https://recreation.gov) RIDB API. Land boundaries via BLM and USFS
ArcGIS services. Geocoding © OpenStreetMap / Nominatim.

---

## License

[MIT](LICENSE) © Wesley Gill
