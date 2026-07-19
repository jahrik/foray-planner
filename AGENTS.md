# AGENTS.md - Foray Planner

Python web app that ranks mushroom-hunting destinations from iNaturalist observation
phenology. Public repo: [jahrik/foray-planner](https://github.com/jahrik/foray-planner).

## Why this app exists

Built for a mycology hobbyist who travels constantly and tracks finds on iNaturalist - turns
iNat phenology into road-trip planning: where to go next to be standing on top of the mushrooms,
and where to sleep for free on BLM/Forest Service land while there. Jobs to be done, in
priority order: (1) when/where are target fungi active now/soon, (2) where to camp for free,
closest to that activity, (3) which trails put you closest to the mushrooms, (4) string several
week-long stays into a sane driving route.

Guiding principles - keep these in mind for any feature work:
- **Free-first.** Rank dispersed camping on public land above paid sites; surface cost when known.
- **No claims.** No edibility/ID/safety claims - and don't *assert* camping legality either. Show
  land ownership + link the official source; informational, not authoritative.
- **Reuse the grid.** Camping and trails hang off the same lat/lng grid + `haversine_km` scoring
  already uses - don't invent a second geography.

## Layout

- `src/foray/config.py` - pydantic-settings (`Settings(BaseSettings)`) with `Home`, `Ingest`,
  `CoverageRegion` models. All config comes from env vars (prefix `FORAY_`, nested
  delimiter `__`) or `.env` file. The runtime location override lives in Postgres
  (`app_location` table, `foray.cache.load_location`/`save_location`), not a file. There's no
  fixed target-genus list (issue #79) - the full Fungi catalog lives in `fungi_genera`
  (refreshed via `foray genera-refresh`), and each device picks its own targets in
  `app_genera`.
- `src/foray/defaults.py` - built-in home location and coverage regions (WA/OR/ID).
  Overridden via `FORAY_COVERAGE` env var.
- `src/foray/inat.py` - throttled pyinaturalist wrapper (observations, species_counts,
  monthly histogram). Descriptive User-Agent; deep-paginates via `id_above`; `_with_retries`
  backs off on transient network errors so one blip doesn't abort a long ingest.
- `src/foray/geocode.py` - resolve a place name (OpenStreetMap Nominatim) or raw `lat,lng`
  to coordinates. Network-mocked in tests.
- `src/foray/cache.py` - Postgres+PostGIS schema (extension + tables created eagerly on every
  `connect()`) + idempotent upserts (`ON CONFLICT`), ingest log. `connect()` takes no DSN by
  default - reads the standard `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` env vars.
- `src/foray/ingest.py` - pulls per seed taxon within the home radius or by coverage region
  (`place_id`). Tags each obs with the **seed** taxon_id (not leaf species) so phenology is
  per foraging target. Region ingest uses chunked inserts (5000 rows) for bounded memory.
- `src/foray/camps.py` - developed-campground ingest from the Recreation.gov **RIDB API**
  (httpx, key from env `RIDB_API_KEY`). Tiles the home radius into <=50-mi query circles,
  dedupes facilities, clips to the true radius with `haversine_km`. Skipped (no-op) when the
  key is unset, so the iNat refresh still works. `free` is only asserted on an explicit
  no-fee signal - never guessed.
- `src/foray/dispersed.py` - dispersed-camping layer from OSM **Overpass** (httpx, no key). Two
  ODbL signals, both cached as `campsites`: reported sites (`kind='reported'` - `tourism=camp_site`
  /`camp_pitch`, `backcountry=yes`) and a proxy (`kind='dispersed'` - `highway=track`/`unclassified`
  within cached `public_land`, via **PostGIS**'s point-in-polygon, ingest-side only so the read path
  stays spatial-free). `free=TRUE` on proxy points (public-land camping is free of
  charge); the *legality* caveat rides on `kind`+UI label, never asserted.
- `src/foray/trails.py` - trail layer from OSM **Overpass** (httpx, no key). One ODbL query pulls
  backcountry paths (`highway=path` -> `kind='path'`, LineString; `footway` is **excluded** - it's
  mostly urban sidewalks), named hiking routes (`route=hiking` relations -> `kind='route'`,
  MultiLineString), and trailheads (`highway=trailhead` nodes -> `kind='trailhead'`, Point).
  Geometry is cached as GeoJSON *text* + bbox + a representative center in `trails`.
- `src/foray/scoring.py` - `build_phenology` (materializes `regions` + `phenology`) and the
  scoring modes: `rank_destinations`, `place_calendar`, `alerts`, `camps_near`, `trails_near`,
  and `plan_route` (greedy multi-stop itinerary). Grid binning is one reusable SQL fragment
  (`_BINNED`). `alerts` includes `place_guess`, `uri`, and `obscured` per observation.
- `src/foray/api.py` - FastAPI: `/api/{config,species,destinations,calendar,alerts,camps,land,
  trails,plan,location,refresh,coverage}` + `/` (serves the built client). Search is **read-only**
  against cached data. `set_location` does not trigger refresh. A `psycopg_pool.ConnectionPool`
  opened/closed via FastAPI `lifespan`; `refresh` runs in a background thread with SSE progress.
- `src/foray/cli.py` - Click CLI: `foray ingest | camps | land | dispersed | trails | refresh |
  plan | serve | openapi`. `ingest --all-regions` is what the scheduler runs.
- `scripts/scheduler.sh` - shell loop running observation ingest (all regions) every N hours
  and layer refresh every M hours. Configurable via `FORAY_INGEST_INTERVAL_HOURS` (default 24)
  and `FORAY_LAYERS_INTERVAL_HOURS` (default 168).
- `frontend/` - the web client: **Vite + TypeScript (strict)**, Leaflet map, split by concern:
  `src/state.ts` (shared `State`, DOM `qs()`/`setStatus()` helpers), `src/map.ts` (Leaflet init,
  theme/tile switching, marker palette, `clear*()` layer helpers), `src/layers.ts` (camps/land/
  trails fetch + render + popups), `src/views.ts` (destinations/calendar/alerts tabs),
  `src/plan.ts` (route planning UI + GPX/JSON export), `src/refresh.ts` (SSE refresh + set-location),
  and `src/main.ts` (DOM wiring/orchestration). `src/api/` holds the typed client (`openapi-fetch`,
  in `client.ts`) + `schema.ts` generated from the backend's OpenAPI via `openapi-typescript` -
  `npm run gen:api` regenerates both; CI fails if that produces a diff, so `schema.ts` never
  drifts from the actual API. `GET /api/coverage` exists on the backend (coverage regions + their
  last-ingest freshness) but has no frontend consumer yet. Builds into `../src/foray/web/dist`. A
  **light/dark theme toggle** is `data-theme`-driven with a `localStorage` preference (default
  **dark**); the basemap follows it (CARTO dark / OSM light).

## Conventions

Follows the global `python` skill: uv, ruff, ty, pytest, and **no single-letter variable
names**. Tests are hermetic - never hit the network (scoring uses fixtures, geocoding is
mocked).

No CORS middleware is configured, which is intentionally safe by omission (no
`Access-Control-Allow-Origin` = no cross-origin JS can read responses). Don't add one later
without scoping `allow_origins` to the real domain.

## Commands

All common operations are centralized in the **Makefile**. It exports PG* env vars and
prepends the nvm Node path automatically.

### Quick start

```bash
make install            # uv sync + frontend npm ci
make db                 # start Postgres+PostGIS
make ingest             # one-shot all-regions ingest + phenology rebuild
make start              # http://localhost:8000 (app + postgres)
make scheduler          # optional: start the background ingest/refresh loop
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
| `make start` | Build + start app + postgres |
| `make scheduler` | Start the background scheduler (observation + layer refresh loops) |
| `make stop` | Stop all containers (including scheduler if running) |
| `make ingest` | One-shot all-regions ingest |
| `make clean` | Tear down containers + volumes |

### Backend CLI

```bash
uv run foray ingest --all-regions  # pull observations for all coverage regions
uv run foray refresh               # ingest + rebuild phenology/regions (all layers)
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
  SQL - never stored redundantly. Change `cell_deg` -> re-run `foray refresh`.
- Location is per-area: changing it (UI `POST /api/location`) immediately runs scoring against
  cached data. The saved override (`app_location` table in Postgres) wins over the env var
  defaults and survives restarts.
- The Postgres database is fully rebuildable via `foray refresh`; connection info comes from
  `PG*` env vars (see `src/foray/cache.py`).
- `campsites` (developed campgrounds) is keyed by `"{source}:{source_id}"` and upserted
  idempotently. Needs `RIDB_API_KEY` (gitignored `.env` locally; env var in prod) - absent,
  camps ingest is a no-op. `free` is nullable: TRUE only on an explicit no-fee signal, else
  NULL (unknown).
- `observations` includes `place_guess`, `uri`, and `obscured` columns enriched from iNat
  during ingest. Existing rows backfill via ON CONFLICT DO UPDATE with COALESCE on next ingest.
- Target genera aren't configured in code - `foray genera-refresh` keeps the full catalog
  synced, `foray ingest` pulls every Fungi observation and resolves each one's own genus from
  its taxon ancestry, and users pick their targets in the search UI (per-device, `app_genera`).

## Not in scope

This is a trip-planning and mapping tool. Make **no** identification, edibility, or safety
claims anywhere - no authored species descriptions, no toxicity/lookalike text. Any such
information is deferred to each taxon's iNaturalist page (`inatUrl()` in `state.ts`), which
the UI links. Keep it that way.
