# AGENTS.md — Foray Planner

Python web app that ranks mushroom-hunting destinations from iNaturalist observation
phenology. Public repo: [jahrik/foray-planner](https://github.com/jahrik/foray-planner).

## Layout

- `src/foray/config.py` — pydantic models (`Config`, `Home`, `Species`) with range
  validation; loads `config.yaml`, applies the `location.json` override, saves locations.
  Config is the file-boundary trust layer — internal scoring types stay dataclasses.
- `src/foray/inat.py` — throttled pyinaturalist wrapper (observations, species_counts,
  monthly histogram). Descriptive User-Agent; deep-paginates via `id_above`; `_with_retries`
  backs off on transient network errors so one blip doesn't abort a long ingest.
- `src/foray/geocode.py` — resolve a place name (OpenStreetMap Nominatim) or raw `lat,lng`
  to coordinates. Network-mocked in tests.
- `src/foray/cache.py` — DuckDB schema + idempotent upserts (`ON CONFLICT`), ingest log.
- `src/foray/ingest.py` — pulls per seed taxon within the home radius; tags each obs with
  the **seed** taxon_id (not leaf species) so phenology is per foraging target.
- `src/foray/camps.py` — developed-campground ingest from the Recreation.gov **RIDB API**
  (httpx, key from env `RIDB_API_KEY`). Tiles the home radius into ≤50-mi query circles,
  dedupes facilities, clips to the true radius with `haversine_km`. Skipped (no-op) when the
  key is unset, so the iNat refresh still works. `free` is only asserted on an explicit
  no-fee signal — never guessed.
- `src/foray/dispersed.py` — dispersed-camping layer from OSM **Overpass** (httpx, no key). Two
  ODbL signals, both cached as `campsites`: reported sites (`kind='reported'` — `tourism=camp_site`
  /`camp_pitch`, `backcountry=yes`) and a proxy (`kind='dispersed'` — `highway=track`/`unclassified`
  ∩ cached `public_land`, via the DuckDB **spatial** extension's point-in-polygon, ingest-side only
  so the read path stays spatial-free). `free=TRUE` on proxy points (public-land camping is free of
  charge); the *legality* caveat rides on `kind`+UI label, never asserted. Best-effort like camps/
  land. iOverlander/The Dyrt are **not** usable (personal-use-only license / no open API).
- `src/foray/trails.py` — trail layer from OSM **Overpass** (httpx, no key). One ODbL query pulls
  backcountry paths (`highway=path` → `kind='path'`, LineString; `footway` is **excluded** — it's
  mostly urban sidewalks, ~6x the volume and irrelevant here), named hiking routes
  (`route=hiking` relations → `kind='route'`, MultiLineString stitched from member ways), and
  trailheads (`highway=trailhead` nodes → `kind='trailhead'`, Point). Geometry is cached as
  GeoJSON *text* + bbox + a representative center in `trails`, so the read path stays spatial-free
  (bbox filter + `haversine_km`); way vertices are thinned. Best-effort like the other OSM/ArcGIS
  ingests; informational only (links the OSM source, no legal-access claim).
- `src/foray/scoring.py` — `build_phenology` (materializes `regions` + `phenology`) and the
  scoring modes: `rank_destinations`, `place_calendar`, `alerts`, `camps_near` (campsites
  near a point, free-first by distance), `trails_near` (trails near a hotspot, nearest first,
  each annotated with the distance to the closest campsite — "park → hike → fungi"), and
  `plan_route` (greedy multi-stop itinerary: pick the top destinations that have a nearby free
  camp, then order them nearest-neighbour from home under a per-leg drive cap). Grid binning
  is one reusable SQL fragment (`_BINNED`).
- `src/foray/api.py` — FastAPI: `/api/{config,species,destinations,calendar,alerts,camps,land,
  trails,plan,location,refresh}` + `/` (serves the built client). `/api/camps` takes a `region_id`
  or a `lat`/`lng` plus `radius_km` + `free_only`; `/api/land` and `/api/trails` take a `region_id`
  or `lat`/`lng` + `radius_km`; `/api/plan` takes `months`/`species` + `max_stops`, `max_drive_km`,
  `camp_radius_km`, `require_free_camp`. One shared DuckDB connection handing out per-request
  cursors; live config is mutable app state; `refresh` runs in a background thread with reads
  guarded while it rebuilds. `destinations`/`plan` default to the current month when none is given.
- `src/foray/cli.py` — `foray ingest | camps | land | dispersed | trails | plan | refresh | serve |
  openapi` (`refresh` does obs + camps + land + dispersed + trails + phenology; `plan` prints a
  greedy itinerary; `openapi` dumps the schema that feeds the frontend type generator).
- `src/foray/web/dist/` — the built client bundle (gitignored; emitted by the frontend build
  and served by FastAPI as static assets at `/assets` + `/`).
- `frontend/` — the web client: **Vite + TypeScript (strict)**, Leaflet map, split by concern:
  `src/state.ts` (shared `State`, DOM `qs()`/`setStatus()` helpers), `src/map.ts` (Leaflet init,
  theme/tile switching, marker palette, `clear*()` layer helpers), `src/layers.ts` (camps/land/
  trails fetch + render + popups), `src/views.ts` (destinations/calendar/alerts tabs),
  `src/plan.ts` (route planning UI + GPX/JSON export), `src/refresh.ts` (SSE refresh + set-location),
  and `src/main.ts` (DOM wiring/orchestration only — kept small on purpose so new features don't
  pile back into one file). `src/api/` holds the typed client + `schema.ts` generated from the
  backend's OpenAPI via `openapi-typescript`, `src/style.css` is the stylesheet. Builds into
  `../src/foray/web/dist`. Marker palette is bright/neon and deliberately non-green (hot magenta =
  strength, electric cyan = recent) so it reads on both basemaps. A **light/dark theme toggle**
  (🌙/☀️, header) is `data-theme`-driven with a `localStorage` preference (default **dark**), set
  before first paint by an inline `<head>` script; the basemap follows it (CARTO dark / OSM light).

## Conventions

Follows the global `python` skill: uv, ruff, ty, pytest, and **no single-letter variable
names**. Tests are hermetic — never hit the network (scoring uses fixtures, geocoding is
mocked).

## Commands

### Running from source (quick start)

```bash
uv sync
export PATH="$HOME/.nvm/versions/node/v24.18.0/bin:$PATH"  # Node via nvm
cd frontend && npm ci && npm run build && cd ..
uv run foray refresh    # pull iNat obs + build phenology (first run hits the network)
uv run foray serve      # http://127.0.0.1:8000  (--host / --port to override)
```

### Backend CLI

```bash
uv run foray ingest      # pull observations only
uv run foray refresh     # ingest + rebuild phenology/regions (obs + camps + land + dispersed)
uv run foray serve --host 0.0.0.0 --port 8000
```

### Frontend dev (hot-reload)

Node is via **nvm**, not on `PATH` by default:
```bash
export PATH="$HOME/.nvm/versions/node/v24.18.0/bin:$PATH"
cd frontend && npm ci
npm run dev        # Vite dev server on :5173, proxying /api to uvicorn on :8000
npm run build      # type-check (tsc --noEmit) + emit the production bundle
npm run gen:api    # regenerate src/api/schema.ts from the live OpenAPI schema
```

For live development, run `uv run foray serve` (backend) and `npm run dev` (client) together.
Rerun `npm run gen:api` after changing any `/api/*` route.

### Tests

Run one test file / one test / by keyword:

```bash
uv run python -m pytest tests/test_scoring.py
uv run python -m pytest tests/test_scoring.py::test_april_ranks_morel_region_first
uv run python -m pytest -k haversine
```

(`uv run pytest` fails locally because venv console-script shebangs point at the old path
`inat-foray-planner` since the repo was renamed; `uv run python -m pytest` works fine. CI
is unaffected. Fix permanently with `uv sync --reinstall`.)

### Gate before finishing

```bash
uv run ruff format . && uv run ruff check . && uv run ty check && uv run python -m pytest
```

When touching `frontend/`, also gate the client (Node ≥ 22; `npm ci` first):

```bash
cd frontend && npm run build   # tsc --noEmit + vite build
```

## Data model notes

- Only `quality_grade=research` counts toward scoring.
- Regions are uniform lat/lng grid cells (`cell_deg`), id = `"{ilat}_{ilng}"`, derived in
  SQL — never stored redundantly. Change `cell_deg` → re-run `foray refresh`.
- Location is per-area: changing it (UI `POST /api/location` or editing `location.json`)
  requires a `refresh` to fetch iNat data for the new radius. `location.json` overrides
  `config.yaml`'s home.
- The cache DB (`data/foray.duckdb`) is gitignored and fully rebuildable via `foray refresh`.
- `campsites` (developed campgrounds) is keyed by `"{source}:{source_id}"` and upserted
  idempotently. Needs `RIDB_API_KEY` (gitignored `.env` locally; env var in prod) — absent,
  camps ingest is a no-op. `free` is nullable: TRUE only on an explicit no-fee signal, else
  NULL (unknown). No legality/claims — surface ownership + link the source, never assert.
- Adding species: edit `data/species_seed.yaml` (resolve taxon_ids via `get_taxa`) then
  refresh.

## Not in scope

This is a trip-planning and mapping tool. Make **no** identification, edibility, or safety
claims anywhere — no authored species descriptions, no toxicity/lookalike text. Any such
information is deferred to each taxon's iNaturalist page (`Species.inat_url`), which the UI
links. Keep it that way.
