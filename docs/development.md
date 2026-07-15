# Development guide

## Prerequisites

- **Python 3.13+** and **[uv](https://docs.astral.sh/uv/)** - uv manages the venv and lockfile
- **Node 22+** via [nvm](https://github.com/nvm-sh/nvm) - not on `PATH` by default, see below
- **Docker / Podman** - runs the local Postgres+PostGIS instance (`docker-compose.yml`); also
  used for the container workflow
- **RIDB_API_KEY** *(optional)* - free key from [Recreation.gov](https://ridb.recreation.gov/landing)
  for campground data; without it the camps ingest step is a no-op and everything else still works

---

## Quick start

```bash
make install            # uv sync + frontend npm ci
make db                 # start Postgres+PostGIS (docker compose)

# Optional: create a .env file with your RIDB key
echo "RIDB_API_KEY=your_key_here" > .env   # omit to skip campground ingest

make ingest             # one-shot all-regions observation ingest + phenology rebuild
make start              # start app + postgres (http://localhost:8000)
make scheduler          # optional: start background ingest/refresh loop
```

The Makefile exports `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` and prepends
the nvm Node path automatically, so you never need to set them manually.

---

## Configuration

All settings come from environment variables (prefix `FORAY_`, nested delimiter `__`) or a `.env` file via pydantic-settings.

| Env var | Default | What it does |
|---|---|---|
| `FORAY_HOME__NAME` | `"Home"` | Display name for your home location |
| `FORAY_HOME__LAT` / `FORAY_HOME__LNG` | 47.6, -122.3 (Seattle) | Home base coordinates |
| `FORAY_HOME__RADIUS_KM` | `150` | How far out to search for destinations |
| `FORAY_CELL_DEG` | `0.25` | Grid cell size in degrees; changing requires a full `foray refresh` |
| `FORAY_INGEST__SINCE_YEAR` | `2015` | How far back to pull iNat observations |
| `FORAY_INGEST__QUALITY_GRADE` | `research` | iNat quality filter |
| `FORAY_INGEST__RECENT_WEEKS` | `4` | Trailing window for the "Fruiting now" live signal |
| `FORAY_SPECIES` | (built-in defaults in `src/foray/defaults.py`) | Curated target taxa list (JSON array) |
| `FORAY_COVERAGE` | (built-in: all 50 US states) | Coverage regions for state-level ingest (JSON array) |
| `FORAY_COUNTRIES` | (built-in: United States) | Country-level regions for single-query observation ingest (JSON array) |
| `FORAY_INGEST_INTERVAL_HOURS` | `24` | Scheduler: hours between observation ingests |
| `FORAY_LAYERS_INTERVAL_HOURS` | `168` | Scheduler: hours between layer refreshes (camps/land/dispersed/trails) |

**Database connection** comes from the standard libpq env vars
(`PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE`), read natively by `psycopg`. Credentials
never belong in a committed file. `docker-compose.yml` + the Makefile export cover local dev;
production gets them injected from AWS Secrets Manager (see `docs/deployment.md`).

**The home-location override** (written by the UI's Set Location form) lives in Postgres now, in
the single-row `app_location` table - not a `location.json` file. It survives `foray refresh`
and container restarts automatically; there's nothing to delete to reset it besides truncating
that table.

---

## Architecture overview

```
FORAY_* env vars (pydantic-settings) + PG* env vars (DB connection)
         |
         v
    Config (pydantic)
         |
    +----+-------------------------------------+
    |                                          |
iNaturalist API          Recreation.gov RIDB API
(pyinaturalist)          OSM Overpass API
ArcGIS BLM/USFS          Nominatim (geocoding)
    |                                          |
    +------------------+-----------------------+
                       |
              Postgres + PostGIS
           (observations, campsites,
            public_land, trails,
            ingest_log, app_location)
                       |
            phenology + regions
            (materialized in SQL)
                       |
                FastAPI /api/*
                       |
           Leaflet + TypeScript client
```

### Data pipeline (decoupled)

Search/scoring is **read-only** against cached data. Data ingestion happens independently:

- **Scheduler service** (`scripts/scheduler.sh`): opt-in via `make scheduler` (docker-compose profile), pulls observations every 24h and refreshes layers (camps/land/dispersed/trails) every 168h
- **Coverage regions**: state-level, using iNat `place_id` for exact administrative boundaries - all 50 US states by default (`FORAY_COVERAGE`), used by `trails --all` (Overpass can't take a whole-country query in one request)
- **Countries**: country-level, one iNat `place_id` per country - United States by default (`FORAY_COUNTRIES`). Adding another country later is just one more entry, no code changes
- **`make ingest`**: one-shot manual ingest for all coverage regions
- **UI Refresh button**: triggers an in-process refresh for the current home radius

The UI's "Set Location" never triggers data fetching. It updates the home coordinates and immediately runs scoring against whatever is already in the database.

---

## CLI reference

```bash
uv run foray ingest              # pull observations for home radius
uv run foray ingest --countries  # pull observations for every configured country (one query each)
uv run foray ingest --all-regions  # pull observations for all coverage regions (state-level)
uv run foray ingest --region "Oregon"  # pull observations for a single region
uv run foray camps               # ingest Recreation.gov campgrounds (needs RIDB_API_KEY)
uv run foray land                # ingest BLM/USFS ownership boundaries (ArcGIS, no key)
uv run foray land --all          # ingest BLM/USFS ownership across all coverage regions, one query
uv run foray dispersed           # ingest OSM dispersed-camping layer (Overpass, no key)
uv run foray trails              # ingest OSM trails: paths, hiking routes, trailheads (Overpass, no key)
uv run foray trails --all        # ingest trails for every coverage region (state), one query each
uv run foray refresh             # ingest (home radius) + camps + land + dispersed + trails + phenology
uv run foray refresh --with camps,trails  # refresh only specific layers
uv run foray refresh --with land,trails --all  # region-scoped land/trails across all coverage; not valid for camps/dispersed (home-radius only)
uv run foray plan                # print a greedy multi-stop trip itinerary
uv run foray serve               # start the FastAPI server (--host / --port to override)
uv run foray openapi             # dump OpenAPI schema (feeds npm run gen:api)
```

`ingest --countries` is what the daily cron runs: one `ingest_region` call per configured
country (place_id-based, no tiling) instead of looping every state - simpler and avoids any
double-counting near state borders. `refresh --with land,trails --all` is what the weekly
layers cron runs: land in one whole-coverage envelope query (ArcGIS's own pagination handles
the volume), trails looped per coverage region since Overpass can't take a query that large in
one request.

`refresh` (no `--all`) is for manual/ad-hoc use against the home radius: it runs ingest + camps
+ land + dispersed + trails + phenology in sequence. `plan` reads the already-cached data and
does no network I/O.

---

## Frontend dev (hot-reload)

Run the backend and the Vite dev server together for live development:

```bash
# Terminal 1 - backend
make db && uv run foray serve

# Terminal 2 - frontend (Vite on :5173, proxies /api/* to uvicorn on :8000)
cd frontend && npm run dev
```

Other frontend commands:

```bash
make frontend      # tsc --noEmit + vite build (the CI gate)
npm run gen:api    # regenerate src/api/schema.ts from the live OpenAPI schema (from frontend/)
```

Rerun `npm run gen:api` after changing any `/api/*` route signature.

---

## Scoring

Three primitives drive all three views:

| Primitive | What it measures |
|---|---|
| **w_pheno** | Fraction of a taxon's regional observations that fall in the target month(s) - "is it in season here?" (0..1) |
| **abundance** | Log-scaled observation count - "how reliably does it show up here?" |
| **recency** | Trailing-weeks observation count - "is it going off right now?" |

**Final score per region** (summed over all target species in the selected months):

```
score = S species [ w_pheno x log1p(month_count) ]
      x (1 + 0.1 x (n_species - 1))   <- diversity bonus
      x (1 + log1p(recent_count))      <- recency boost
```

Scores are normalized 0..1 against the top region. The calendar view fixes the region axis and
shows per-month totals. The alerts view fixes species + recency, ignoring the month selection.

---

## Postgres schema

| Table | Key | Contents |
|---|---|---|
| `observations` | `(id)` | Raw iNat research-grade observations: lat, lng, observed_on, taxon_id, place_guess, uri, obscured |
| `taxa` | `taxon_id` | taxon_id -> name/common_name mapping |
| `phenology` | `(region_id, taxon_id, month)` | Materialized per-(region, taxon, month) observation counts |
| `regions` | `region_id` | Grid cell summaries: center coords, total obs count, distinct taxa |
| `campsites` | `id` (`"{source}:{source_id}"`) | Developed campgrounds (RIDB) + OSM reported/dispersed sites |
| `public_land` | `id` (`"{source}:{source_id}"`) | BLM/USFS ownership polygons - GeoJSON text + bbox columns |
| `trails` | `id` (`"{source}:{osm_type}/{osm_id}"`) | OSM trails/routes/trailheads - GeoJSON text + bbox columns |
| `ingest_log` | - | Per-run progress records for refresh stages |
| `app_location` | single row | The UI's "Set location" override |

`phenology`/`regions` are dynamically (re)materialized by `foray ingest` and `foray refresh`;
every other table is created by `foray.cache.SCHEMA` (which also enables the `postgis` extension,
used only by the dispersed-camping ingest's point-in-polygon join - the read path never needs
PostGIS geometry types). The database is fully rebuildable with `foray refresh`. Change
`FORAY_CELL_DEG` and re-run refresh to rebuild with a different grid resolution.

---

## Adding or changing target species

Edit `src/foray/defaults.py` or override via the `FORAY_SPECIES` env var (JSON array). Each entry needs:

```json
{"taxon_id": 56830, "name": "Morchella", "common_name": "Morels", "rank": "genus"}
```

Taxon IDs come from iNaturalist - look them up on the website or via
`pyinaturalist.get_taxa(q="Morchella", rank="genus")`. Genus-level only by convention
(coarser = more observations = better phenology signal).

**Hard rule:** no authored descriptions, edibility claims, or lookalike text anywhere in this
codebase. The UI links each taxon to its iNaturalist page. Keep it that way.

After editing, run `make ingest` to re-ingest.

---

## Linting and testing

The full local CI gate (starts Postgres if needed, runs lint + type-check + tests):

```bash
make check
```

Or run pieces individually:

```bash
make lint          # ruff format + ruff check + ty check
make test          # pytest (starts Postgres automatically)
make frontend      # frontend type-check + bundle (after frontend or API changes)
```

Focused test runs:

```bash
uv run pytest tests/test_scoring.py
uv run pytest tests/test_scoring.py::test_april_ranks_morel_region_first
uv run pytest -k haversine
```

Tests hit no network beyond the local/CI Postgres service container - the same boundary the
suite already accepted as "hermetic" before this moved off DuckDB. Isolation is a shared
connection + `TRUNCATE`-before-each-test (see `tests/conftest.py`), not per-test rollback or a
fresh schema per test - see the fixture's docstring for why. Geocoding and HTTP calls are mocked
with `httpx.MockTransport`.

---

## Docker build

```bash
docker build -t local/foray-planner:dev .
```

Or use the full stack:

```bash
make start         # builds + starts app + postgres
make scheduler     # optional: starts the background ingest/refresh loop
make stop          # stop all containers (including scheduler if running)
make clean         # tear down containers + volumes
```

The Dockerfile uses a three-stage build:
1. `node:22-slim` - builds the Vite/TypeScript client bundle
2. `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` - installs Python deps with uv
3. `python:3.13-slim-bookworm` - lean runtime, non-root `foray` user, no local volume (DB is
   Postgres, reached via env vars)

See [deployment.md](deployment.md) for production details.
