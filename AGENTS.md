# AGENTS.md - Foray Planner

Python web app that ranks mushroom-hunting destinations from iNaturalist observation
phenology. Public repo: [jahrik/foray-planner](https://github.com/jahrik/foray-planner).

## Layout

- `src/foray/config.py` - pydantic models (`Config`, `Home`, `Species`) with range
  validation; loads `config.yaml`. Config is the file-boundary trust layer - internal scoring
  types stay dataclasses. The runtime location override lives in Postgres now (`app_location`
  table, `foray.cache.load_location`/`save_location`), not a file - no DB connection here.
- `src/foray/inat.py` - throttled pyinaturalist wrapper (observations, species_counts,
  monthly histogram). Descriptive User-Agent; deep-paginates via `id_above`; `_with_retries`
  backs off on transient network errors so one blip doesn't abort a long ingest.
- `src/foray/geocode.py` - resolve a place name (OpenStreetMap Nominatim) or raw `lat,lng`
  to coordinates. Network-mocked in tests.
- `src/foray/cache.py` - Postgres+PostGIS schema (extension + tables created eagerly on every
  `connect()`) + idempotent upserts (`ON CONFLICT`), ingest log. `connect()` takes no DSN by
  default - reads the standard `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` env vars.
- `src/foray/ingest.py` - pulls per seed taxon within the home radius; tags each obs with
  the **seed** taxon_id (not leaf species) so phenology is per foraging target.
- `src/foray/camps.py` - developed-campground ingest from the Recreation.gov **RIDB API**
  (httpx, key from env `RIDB_API_KEY`). Tiles the home radius into ≤50-mi query circles,
  dedupes facilities, clips to the true radius with `haversine_km`. Skipped (no-op) when the
  key is unset, so the iNat refresh still works. `free` is only asserted on an explicit
  no-fee signal - never guessed.
- `src/foray/dispersed.py` - dispersed-camping layer from OSM **Overpass** (httpx, no key). Two
  ODbL signals, both cached as `campsites`: reported sites (`kind='reported'` - `tourism=camp_site`
  /`camp_pitch`, `backcountry=yes`) and a proxy (`kind='dispersed'` - `highway=track`/`unclassified`
  ∩ cached `public_land`, via **PostGIS**'s point-in-polygon, ingest-side only so the read path
  stays spatial-free). `free=TRUE` on proxy points (public-land camping is free of
  charge); the *legality* caveat rides on `kind`+UI label, never asserted. Best-effort like camps/
  land. iOverlander/The Dyrt are **not** usable (personal-use-only license / no open API).
- `src/foray/trails.py` - trail layer from OSM **Overpass** (httpx, no key). One ODbL query pulls
  backcountry paths (`highway=path` → `kind='path'`, LineString; `footway` is **excluded** - it's
  mostly urban sidewalks, ~6x the volume and irrelevant here), named hiking routes
  (`route=hiking` relations → `kind='route'`, MultiLineString stitched from member ways), and
  trailheads (`highway=trailhead` nodes → `kind='trailhead'`, Point). Geometry is cached as
  GeoJSON *text* + bbox + a representative center in `trails`, so the read path stays spatial-free
  (bbox filter + `haversine_km`); way vertices are thinned. Best-effort like the other OSM/ArcGIS
  ingests; informational only (links the OSM source, no legal-access claim).
- `src/foray/scoring.py` - `build_phenology` (materializes `regions` + `phenology`) and the
  scoring modes: `rank_destinations`, `place_calendar`, `alerts`, `camps_near` (campsites
  near a point, free-first by distance), `trails_near` (trails near a hotspot, nearest first,
  each annotated with the distance to the closest campsite - "park → hike → fungi"), and
  `plan_route` (greedy multi-stop itinerary: pick the top destinations that have a nearby free
  camp, then order them nearest-neighbour from home under a per-leg drive cap). Grid binning
  is one reusable SQL fragment (`_BINNED`).
- `src/foray/api.py` - FastAPI: `/api/{config,species,destinations,calendar,alerts,camps,land,
  trails,plan,location,refresh}` + `/` (serves the built client). `/api/camps` takes a `region_id`
  or a `lat`/`lng` plus `radius_km` + `free_only`; `/api/land` and `/api/trails` take a `region_id`
  or `lat`/`lng` + `radius_km`; `/api/plan` takes `months`/`species` + `max_stops`, `max_drive_km`,
  `camp_radius_km`, `require_free_camp`. A `psycopg_pool.ConnectionPool` opened/closed via
  FastAPI `lifespan`, one pooled connection per request; live config is mutable app state;
  `refresh` runs in a background thread on its own pooled connection for the duration, with
  reads guarded while it rebuilds (Postgres MVCC handles the actual read/write concurrency -
  no DuckDB-style single-writer serialization needed). `destinations`/`plan` default to the
  current month when none is given.
- `src/foray/cli.py` - `foray ingest | camps | land | dispersed | trails | plan | refresh | serve |
  openapi` (`refresh` does obs + camps + land + dispersed + trails + phenology; `plan` prints a
  greedy itinerary; `openapi` dumps the schema that feeds the frontend type generator).
- `src/foray/web/dist/` - the built client bundle (gitignored; emitted by the frontend build
  and served by FastAPI as static assets at `/assets` + `/`).
- `frontend/` - the web client: **Vite + TypeScript (strict)**, Leaflet map, split by concern:
  `src/state.ts` (shared `State`, DOM `qs()`/`setStatus()` helpers), `src/map.ts` (Leaflet init,
  theme/tile switching, marker palette, `clear*()` layer helpers), `src/layers.ts` (camps/land/
  trails fetch + render + popups), `src/views.ts` (destinations/calendar/alerts tabs),
  `src/plan.ts` (route planning UI + GPX/JSON export), `src/refresh.ts` (SSE refresh + set-location),
  and `src/main.ts` (DOM wiring/orchestration only - kept small on purpose so new features don't
  pile back into one file). `src/api/` holds the typed client + `schema.ts` generated from the
  backend's OpenAPI via `openapi-typescript`, `src/style.css` is the stylesheet. Builds into
  `../src/foray/web/dist`. Marker palette is bright/neon and deliberately non-green (hot magenta =
  strength, electric cyan = recent) so it reads on both basemaps. A **light/dark theme toggle**
  (🌙/☀️, header) is `data-theme`-driven with a `localStorage` preference (default **dark**), set
  before first paint by an inline `<head>` script; the basemap follows it (CARTO dark / OSM light).

## Conventions

Follows the global `python` skill: uv, ruff, ty, pytest, and **no single-letter variable
names**. Tests are hermetic - never hit the network (scoring uses fixtures, geocoding is
mocked).

## Commands

All common operations are centralized in the **Makefile**. It exports PG* env vars and
prepends the nvm Node path automatically.

### Quick start

```bash
make install            # uv sync + frontend npm ci
make db                 # start Postgres+PostGIS
uv run foray refresh    # pull iNat obs + build phenology (first run hits the network)
make start              # http://localhost:8000 (app + scheduler + postgres)
```

### Makefile targets

| Target | What it does |
|---|---|
| `make db` | Start Postgres+PostGIS (docker compose), wait for ready |
| `make install` | `uv sync` + `cd frontend && npm ci` |
| `make lint` | `ruff format` + `ruff check` + `ty check` |
| `make test` | Start Postgres if needed, then `pytest` |
| `make check` | `lint` + `test` (the full local CI gate) |
| `make frontend` | Build the Vite/TypeScript client bundle |
| `make start` | Build + start the full stack (app + scheduler + postgres) |
| `make stop` | Stop all containers |
| `make ingest` | One-shot all-regions ingest |
| `make clean` | Tear down containers + volumes |

### Backend CLI

```bash
uv run foray ingest      # pull observations only
uv run foray refresh     # ingest + rebuild phenology/regions (obs + camps + land + dispersed)
uv run foray serve --host 0.0.0.0 --port 8000
```

### Frontend dev (hot-reload)

```bash
# Terminal 1 - backend
make db && uv run foray serve

# Terminal 2 - frontend (Vite on :5173, proxies /api/* to uvicorn on :8000)
cd frontend && npm run dev
```

Rerun `npm run gen:api` (from `frontend/`) after changing any `/api/*` route.

### Tests

Run one test file / one test / by keyword:

```bash
uv run pytest tests/test_scoring.py
uv run pytest tests/test_scoring.py::test_april_ranks_morel_region_first
uv run pytest -k haversine
```

### Gate before finishing

```bash
make check
```

When touching `frontend/`, also run:

```bash
make frontend
```

## Data model notes

- Only `quality_grade=research` counts toward scoring.
- Regions are uniform lat/lng grid cells (`cell_deg`), id = `"{ilat}_{ilng}"`, derived in
  SQL - never stored redundantly. Change `cell_deg` → re-run `foray refresh`.
- Location is per-area: changing it (UI `POST /api/location`) requires a `refresh` to fetch
  iNat data for the new radius. The saved override (`app_location` table in Postgres) wins
  over `config.yaml`'s home and survives restarts.
- The Postgres database is fully rebuildable via `foray refresh`; connection info comes from
  `PG*` env vars, never `config.yaml` (see `src/foray/cache.py`).
- `campsites` (developed campgrounds) is keyed by `"{source}:{source_id}"` and upserted
  idempotently. Needs `RIDB_API_KEY` (gitignored `.env` locally; env var in prod) - absent,
  camps ingest is a no-op. `free` is nullable: TRUE only on an explicit no-fee signal, else
  NULL (unknown). No legality/claims - surface ownership + link the source, never assert.
- Adding species: edit `src/foray/defaults.py` (resolve taxon_ids via `get_taxa`) or set
  `FORAY_SPECIES` env var (JSON array), then refresh.

## Not in scope

This is a trip-planning and mapping tool. Make **no** identification, edibility, or safety
claims anywhere - no authored species descriptions, no toxicity/lookalike text. Any such
information is deferred to each taxon's iNaturalist page (`Species.inat_url`), which the UI
links. Keep it that way.
