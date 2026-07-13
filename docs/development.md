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
uv sync

# Node is managed by nvm, not on PATH by default:
export PATH="$HOME/.nvm/versions/node/v24.18.0/bin:$PATH"
cd frontend && npm ci && npm run build && cd ..

# Postgres+PostGIS for local dev (same image CI uses)
docker compose up -d postgres
export PGHOST=localhost PGPORT=5432 PGUSER=foray PGPASSWORD=foray PGDATABASE=foray

# Optional: create a .env file with your RIDB key
echo "RIDB_API_KEY=your_key_here" > .env   # omit to skip campground ingest

uv run foray refresh    # pull iNat data + build phenology (minutes; hits the network)
uv run foray serve      # http://127.0.0.1:8000
```

---

## Configuration

**`config.yaml`** - default settings. Edit this to change your home base.

| Key | Default | What it does |
|---|---|---|
| `home.name` | `"Home"` | Display name for your home location |
| `home.lat` / `home.lng` | 47.6, -122.3 (Seattle) | Home base coordinates - the map centers here and destinations are ranked relative to it |
| `home.radius_km` | `400` | How far out to search for destinations |
| `regions.cell_deg` | `0.5` | Grid cell size in degrees (~55 km at mid-latitudes); changing this requires a full `foray refresh` |
| `ingest.since_year` | `2015` | How far back to pull iNat observations |
| `ingest.quality_grade` | `research` | iNat quality filter - `research` only (verifier-confirmed, mapped coordinates) |
| `ingest.recent_weeks` | `4` | Trailing window for the "Fruiting now" live signal |
| `paths.species_seed` | `data/species_seed.yaml` | Curated target taxa list |

**Database connection** is *not* a config.yaml key - it comes from the standard libpq env vars
(`PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE`), read natively by `psycopg`. Credentials
never belong in a committed YAML file. `docker-compose.yml` + the export above cover local dev;
production gets them injected from AWS Secrets Manager (see `docs/deployment.md`).

**The home-location override** (written by the UI's Set Location form) lives in Postgres now, in
the single-row `app_location` table - not a `location.json` file. It survives `foray refresh`
and container restarts automatically; there's nothing to delete to reset it besides truncating
that table.

---

## CLI reference

```bash
uv run foray ingest      # pull iNat observations only (no phenology rebuild)
uv run foray camps       # ingest Recreation.gov campgrounds (needs RIDB_API_KEY)
uv run foray land        # ingest BLM/USFS ownership boundaries (ArcGIS, no key)
uv run foray dispersed   # ingest OSM dispersed-camping layer (Overpass, no key)
uv run foray trails      # ingest OSM trails: paths, hiking routes, trailheads (Overpass, no key)
uv run foray refresh     # all of the above + rebuild phenology/regions tables
uv run foray plan        # print a greedy multi-stop trip itinerary (--months, --max-stops,
                         #   --max-drive-km, --any-camp)
uv run foray serve       # start the FastAPI server (--host / --port to override)
uv run foray openapi     # dump OpenAPI schema (feeds npm run gen:api)
```

`refresh` is the normal daily/weekly operation: it runs ingest → camps → land → dispersed →
trails → phenology in sequence, logging progress per stage. `plan` reads the already-refreshed
cache and does no network I/O.

---

## Frontend dev (hot-reload)

Run the backend and the Vite dev server together for live development:

```bash
# Terminal 1 - backend
uv run foray serve

# Terminal 2 - frontend
export PATH="$HOME/.nvm/versions/node/v24.18.0/bin:$PATH"
cd frontend && npm ci
npm run dev        # Vite on :5173, proxies /api/* to uvicorn on :8000
```

Other frontend commands:

```bash
npm run build      # tsc --noEmit + vite build (the CI gate)
npm run gen:api    # regenerate src/api/schema.ts from the live OpenAPI schema
```

Rerun `npm run gen:api` after changing any `/api/*` route signature.

---

## Architecture overview

```
config.yaml (static settings) + PG* env vars (DB connection)
         │
         ▼
    Config (pydantic)
         │
    ┌────┴─────────────────────────────────┐
    │                                      │
iNaturalist API          Recreation.gov RIDB API
(pyinaturalist)          OSM Overpass API
ArcGIS BLM/USFS          Nominatim (geocoding)
    │                                      │
    └────────────────┬─────────────────────┘
                     │
            Postgres + PostGIS
         (observations, campsites,
          public_land, ingest_log,
          app_location)
                     │
          phenology + regions
          (materialized in SQL)
                     │
              FastAPI /api/*
                     │
         Leaflet + TypeScript client
```

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
score = Σ species [ w_pheno × log1p(month_count) ]
      × (1 + 0.1 × (n_species − 1))   ← diversity bonus
      × (1 + log1p(recent_count))      ← recency boost
```

Scores are normalized 0..1 against the top region. The calendar view fixes the region axis and
shows per-month totals. The alerts view fixes species + recency, ignoring the month selection.

---

## Postgres schema

| Table | Key | Contents |
|---|---|---|
| `observations` | `(id)` | Raw iNat research-grade observations: lat, lng, observed_on, taxon_id |
| `taxa` | `taxon_id` | taxon_id → name/common_name mapping |
| `phenology` | `(region_id, taxon_id, month)` | Materialized per-(region, taxon, month) observation counts |
| `regions` | `region_id` | Grid cell summaries: center coords, total obs count, distinct taxa |
| `campsites` | `id` (`"{source}:{source_id}"`) | Developed campgrounds (RIDB) + OSM reported/dispersed sites |
| `public_land` | `id` (`"{source}:{source_id}"`) | BLM/USFS ownership polygons - GeoJSON text + bbox columns |
| `trails` | `id` (`"{source}:{osm_type}/{osm_id}"`) | OSM trails/routes/trailheads - GeoJSON text + bbox columns |
| `ingest_log` | - | Per-run progress records for refresh stages |
| `app_location` | single row | The UI's "Set location" override |

`phenology`/`regions` are dynamically (re)materialized by `foray refresh`; every other table is
created by `foray.cache.SCHEMA` (which also enables the `postgis` extension, used only by the
dispersed-camping ingest's point-in-polygon join - the read path never needs PostGIS geometry
types). The database is fully rebuildable with `foray refresh`. Change `cell_deg` in
`config.yaml` and re-run refresh to rebuild with a different grid resolution.

---

## Adding or changing target species

Edit [`data/species_seed.yaml`](../data/species_seed.yaml). Each entry needs:

```yaml
- { taxon_id: 56830, name: Morchella, common_name: Morels, rank: genus }
```

Taxon IDs come from iNaturalist - look them up on the website or via
`pyinaturalist.get_taxa(q="Morchella", rank="genus")`. Genus-level only by convention
(coarser = more observations = better phenology signal).

**Hard rule:** no authored descriptions, edibility claims, or lookalike text anywhere in this
codebase. The UI links each taxon to its iNaturalist page. Keep it that way.

After editing, run `foray refresh` to re-ingest.

---

## Linting and testing

Tests need the local Postgres running (`docker compose up -d postgres`) and the `PG*` env vars
exported (see Quick start above). Gate before every PR:

```bash
uv run ruff format . && uv run ruff check . && uv run ty check && uv run pytest
```

Frontend gate (run after any frontend or API change):

```bash
export PATH="$HOME/.nvm/versions/node/v24.18.0/bin:$PATH"
cd frontend && npm run build
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
# Build locally
docker build -t local/foray-planner:dev .

# One-off: initial data refresh (needs a reachable Postgres - see docker-compose.yml)
docker run --rm \
  -e PGHOST=host.docker.internal -e PGUSER=foray -e PGPASSWORD=foray -e PGDATABASE=foray \
  -e RIDB_API_KEY=$RIDB_API_KEY \
  local/foray-planner:dev \
  foray --config config.docker.yaml refresh

# Serve
docker run -p 8000:8000 \
  -e PGHOST=host.docker.internal -e PGUSER=foray -e PGPASSWORD=foray -e PGDATABASE=foray \
  local/foray-planner:dev
```

The Dockerfile uses a three-stage build:
1. `node:22-slim` - builds the Vite/TypeScript client bundle
2. `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` - installs Python deps with uv
3. `python:3.13-slim-bookworm` - lean runtime, non-root `foray` user, no local volume (DB is
   Postgres, reached via env vars)

See [deployment.md](deployment.md) for production details.
