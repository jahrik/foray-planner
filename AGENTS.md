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
- **No real road routing yet.** `plan_route`'s corridor is a straight-line (great-circle chord)
  buffer, not an actual road route - there's no routing engine (OSRM/Valhalla/etc.) wired up.
  Self-hosting one is a deliberate future follow-up once a region scope is picked, not something
  to bolt on ad hoc.

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
- `src/foray/inat.py` - throttled pyinaturalist wrapper (observations, fungi-genera
  catalog, photos). Descriptive User-Agent; deep-paginates via `id_above`; `_with_retries`
  backs off on transient network errors so one blip doesn't abort a long ingest.
- `src/foray/geocode.py` - resolve a place name (OpenStreetMap Nominatim) or raw `lat,lng`
  to coordinates. Network-mocked in tests.
- `src/foray/geo.py` - pure lat/lng math shared by scoring + the ingest layer: `haversine_km`
  (canonical distance), `bbox_around` (the flat-degree disk bbox every `*_near` query
  prefilters with), `KM_PER_DEG_LAT`, and the corridor tangent-plane projection helpers.
- `src/foray/http.py` - shared HTTP plumbing for the external-data modules: `USER_AGENT`,
  `Throttle` (process-wide request pacer), `retry_after_seconds` (`Retry-After` parsing +
  capped backoff), and `SOURCE_ERRORS` (the "log + degrade to empty" exception tuple).
- `src/foray/overpass.py` - shared OSM Overpass client (endpoint, `around()`/`bbox()` filter
  fragments, `post()` with 429/504 backoff) used by `dispersed.py` and `trails.py`.
- `src/foray/cache.py` - Postgres schema (tables created eagerly on every `connect()`) +
  idempotent upserts (`ON CONFLICT`), ingest log. `connect()` takes no DSN by default - reads
  the standard `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` env vars.
- `src/foray/ingest_base.py` - `run_area_ingest(...)`: the shared skeleton (skip-if-covered ->
  fetch -> upsert -> record) behind the four home-radius area ingests (campgrounds, dispersed,
  land, trails). A new area source is a fetch function + an upsert function.
- `src/foray/ingest.py` - pulls per seed taxon within the home radius or by coverage region
  (`place_id`). Tags each obs with the **seed** taxon_id (not leaf species) so phenology is
  per foraging target. `ingest` / `ingest_region` share `_consume_observations` (scan ->
  resolve genus -> chunked upsert, 5000 rows, with progress + abort) for bounded memory.
  `revalidate()` is a separate, recurring re-check pass: a handful of fungal genus names are
  homonyms of common animal genera (fungal *Olla* vs. the ladybug genus, etc), so observations
  occasionally get cached under the wrong (non-fungal) taxon_id and never self-correct since
  `ingest`/`ingest_region` only ever revisit a narrow incremental overlap window. It targets
  only genus taxon_ids flagged by `cache.suspect_genus_taxon_ids` (cached-count vs.
  `fungi_genera.observations_count`, DB-only, no iNat call) and re-fetches just those cached
  observations to purge/reassign anything no longer Fungi. `resync()` is the slower complement:
  a whole-table grind, one small batch per call, oldest/never-live-checked first
  (`cache.stale_observation_ids`, driven by the `revalidated_at` column both functions stamp via
  `cache.mark_revalidated`) - it's the only path that eventually re-verifies every column of
  every row, including `obscured` (never set by the bulk historical import) and
  misidentifications too rare within their genus for `revalidate`'s ratio to flag. Both share
  the actual re-check/purge/reassign logic (`_recheck_ids`).
- `src/foray/camps.py` - developed-campground ingest from the Recreation.gov **RIDB API**
  (httpx, key from env `RIDB_API_KEY`). Tiles the home radius into <=50-mi query circles,
  dedupes facilities, clips to the true radius with `haversine_km`. Skipped (no-op) when the
  key is unset, so the iNat refresh still works. `free` is only asserted on an explicit
  no-fee signal - never guessed.
- `src/foray/dispersed.py` - dispersed-camping layer from OSM **Overpass** (httpx, no key).
  One ODbL signal, cached as `campsites` (`kind='reported'` - `tourism=camp_site`/`camp_pitch`,
  `backcountry=yes`). `free=TRUE` only on an explicit no-fee tag, never guessed; the *legality*
  caveat rides on `kind`+UI label, never asserted. (A `public_land`-proxy signal - unmapped roads
  within public land, inferred as likely dispersed sites - was scoped but never implemented; see
  issue #110.)
- `src/foray/trails.py` - trail layer from OSM **Overpass** (httpx, no key). One ODbL query pulls
  backcountry paths (`highway=path` -> `kind='path'`, LineString; `footway` is **excluded** - it's
  mostly urban sidewalks), named hiking routes (`route=hiking` relations -> `kind='route'`,
  MultiLineString), and trailheads (`highway=trailhead` nodes -> `kind='trailhead'`, Point).
  Geometry is cached as GeoJSON *text* + bbox + a representative center in `trails`.
- `src/foray/` scoring package (was one `scoring.py`; `scoring.py` is now a back-compat
  re-export shim, to be removed):
  - `models.py` - the result dataclasses (`SpeciesHit`, `RegionScore`, `CampSite`, `LandUnit`,
    `Trail`, `TrailPath`, `Stop`, `TripPlan`); mirrored by `api_models.py`.
  - `regions.py` - `build_phenology` (materializes `regions` + `phenology`) plus the
    materialized-table helpers.
  - `ranking.py` - `rank_destinations` / `rank_destinations_corridor` (fix months -> rank
    regions).
  - `queries.py` - the point-and-radius read repository: `camps_near`, `land_near`,
    `trails_near`, `get_trail`, `nearest_trail`, `place_calendar`, `recent_observations`,
    `alerts` (includes `place_guess` / `uri` / `obscured` per obs), `precise_observations`.
  - `planner.py` - `plan_route` (start -> destination corridor trip: stops along the
    straight-line buffer, each annotated with a nearby camp *and* trail, ordered by progress;
    auto-picks a destination when the caller doesn't - see "no real road routing yet" below).
  - `_scoring_sql.py` - the SQL fragments shared by the three query modules (grid binning
    `BINNED`, the decoy-aware center expressions, the `taxon_id` / `IN (...)` helpers,
    `genus_name_map`).
- `src/foray/api/` - FastAPI package (was one `api.py`; issue #242 Part 1e). `/api/{config,
  genera,destinations,calendar,alerts,camps,land,trails,plan,location,refresh,coverage}` + `/`
  (serves the built client). Search is **read-only** against cached data. `set_location` does not
  trigger refresh. A `psycopg_pool.ConnectionPool` opened/closed via FastAPI `lifespan`; `refresh`
  runs in a background thread with SSE progress.
  - `app.py` - `create_app()`: builds the `FastAPI`, opens the pool + `AppState` onto `app.state`,
    registers the routers in OpenAPI-schema order (`foray openapi` output is drift-checked).
  - `routes/*.py` - one `APIRouter` per domain (`config`, `genera`, `coverage`, `destinations`,
    `layers`, `plan`, `location`, `refresh`, `index`).
  - `deps.py` - shared request helpers as module functions: `get_pool` / `get_state` accessors,
    anonymous device-id resolution, `resolve_home` / `resolve_genera`, `parse_months` /
    `parse_species`, `region_center`, rate limiting.
  - `state.py` - the `AppState` dataclass. `security.py` - CSP + security headers + body-size cap.
    `refresh_runner.py` - the API-side wrapper around `run_home_refresh`: background thread,
    shared HTTP client, cancellation, SSE progress broadcast. `paths.py` - `web/dist`.
- `src/foray/refresh.py` - the ingest-refresh sequence shared by the CLI and API paths:
  `run_home_refresh(cfg, conn, layers, *, client, abort_event, progress_cb)` (home-radius
  ingest per layer + phenology rebuild), plus `REFRESH_LAYERS` / `REFRESH_TARGETS` and
  `parse_month_list`. The CLI's coverage-wide `foray refresh --all` is a separate per-region
  sequence and stays in `cli.py`.
- `src/foray/logging_config.py` - `setup_logging(level)` (env `FORAY_LOG_LEVEL`, default INFO),
  called by both the CLI group callback and `create_app`.
- `src/foray/cli.py` - Click CLI: `foray ingest | camps | land | dispersed | trails | refresh |
  revalidate | resync | backfill-elevation | plan | serve | openapi`. `ingest --all-regions` is
  what the scheduler runs. `backfill-elevation` fills `observations.elevation_m` for the backlog
  (ingest enriches new rows inline via Open-Meteo); destination cards show the region's mean.
  `resync --until-done` loops batch after batch until the whole cache is caught up
  (`make resync ARGS="--until-done --batch-size 20000"`) - a deliberate one-off catch-up run,
  not the small-batch/hourly default the scheduler uses.
- `scripts/scheduler.sh` - shell loop running observation ingest (all regions), layer refresh,
  observation revalidation (`foray revalidate`, see `ingest.py`), the whole-table resync
  grind (`foray resync --batch-size N`), and the elevation backfill drain
  (`foray backfill-elevation --limit N`, issue #36 - Open-Meteo rate-limits a burst so each
  pass only does a few hundred rows), each on their own N-hour interval. Configurable via
  `FORAY_INGEST_INTERVAL_HOURS` (default 24), `FORAY_LAYERS_INTERVAL_HOURS` (default 168),
  `FORAY_REVALIDATE_INTERVAL_HOURS` (default 168), `FORAY_RESYNC_INTERVAL_HOURS` (default 1),
  `FORAY_RESYNC_BATCH_SIZE` (default 2000), `FORAY_ELEVATION_INTERVAL_HOURS` (default 1), and
  `FORAY_ELEVATION_LIMIT` (default 20000 - an upper bound; a run stops earlier when Open-Meteo
  rate-limits it).
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
make db                 # start Postgres
make ingest             # one-shot all-regions ingest + phenology rebuild
make start              # http://localhost:8000 (app + postgres)
make scheduler          # optional: start the background ingest/refresh loop
```

### Makefile targets

| Target | What it does |
|---|---|
| `make db` | Start Postgres (docker compose), wait for ready |
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
- The Postgres database is fully rebuildable via `foray refresh` - with one exception:
  `app_location` (per-device "Set location" override, `src/foray/cache.py`) is genuinely
  user-authored state that no ingest path regenerates. In prod, it lives on the DigitalOcean
  managed Postgres cluster provisioned by `infra/ansible/tasks/provision/database.yml`
  (`digitalocean.cloud.database_cluster`), so it's covered by whatever automatic
  snapshot/backup policy that managed offering applies by default - this repo doesn't
  configure, verify, or restore-test that policy, so treat it as unconfirmed rather than a
  guarantee. There is no `pg_dump` cron or other app-level backup for it. Local/self-hosted
  `docker-compose` deployments have it even less covered: its only persistence is the
  `foray-postgres-data` named volume (`docker-compose.yml`), with no backup at all. Either
  way, if the underlying storage is lost, every visitor's saved location silently reverts to
  the env-var default home with no warning - see issue #114.
- Connection info comes from `PG*` env vars (see `src/foray/cache.py`).
- `campsites` (developed campgrounds) is keyed by `"{source}:{source_id}"` and upserted
  idempotently. Needs `RIDB_API_KEY` (gitignored `.env` locally; env var in prod) - absent,
  camps ingest is a no-op. `free` is nullable: TRUE only on an explicit no-fee signal, else
  NULL (unknown).
- `observations` includes `place_guess`, `uri`, and `obscured` columns enriched from iNat
  during ingest. Existing rows backfill via ON CONFLICT DO UPDATE with COALESCE on next ingest.
  `elevation_m` (issue #36) is filled separately, post-ingest, from Open-Meteo's DEM
  (`ingest.backfill_elevations` / `foray backfill-elevation`) - NULL until looked up.
- Target genera aren't configured in code - `foray genera-refresh` keeps the full catalog
  synced, `foray ingest` pulls every Fungi observation and resolves each one's own genus from
  its taxon ancestry, and users pick their targets in the search UI (per-device, `app_genera`).
- Droplet CPU/memory/disk alerting (issue #84 - a past ENOSPC incident went unnoticed until the
  app broke) is provisioned as code: `infra/ansible/tasks/provision/monitoring.yml` creates DO
  monitoring alert policies via `digitalocean.cloud.monitoring_alert_policy`, opt-in behind the
  `foray_alert_email` var (`FORAY_ALERT_EMAIL` env; unset skips creation). A Cloudflare health
  check is a further, complementary layer this repo doesn't automate - set one up manually in
  the Cloudflare dashboard (Traffic -> Health Checks) against `https://forayplanner.com/` if
  wanted; nothing here depends on it.

## Not in scope

This is a trip-planning and mapping tool. Make **no** identification, edibility, or safety
claims anywhere - no authored species descriptions, no toxicity/lookalike text. Any such
information is deferred to each taxon's iNaturalist page (`inatUrl()` in `state.ts`), which
the UI links. Keep it that way.
