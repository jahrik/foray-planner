# Configuration

Everything configurable is an environment variable. There are three families, and it helps to
know which one a setting belongs to:

1. **`FORAY_*`** settings, parsed into one typed object by
   [`config.py`](../src/foray/config.py) (pydantic-settings).
2. **`PG*`** database connection variables, read directly by libpq/psycopg.
3. **A few standalone variables** read where they are used (`RIDB_API_KEY`,
   `FORAY_LOG_LEVEL`, ...).

> **Secrets never go in a committed file.** Locally they live in a gitignored `.env`; in
> production they come from the Ansible-managed environment file on the host. See
> [deployment.md](deployment.md).

## How values are resolved

- Variables use the prefix `FORAY_`. A nested setting joins its path with a double underscore, so
  `home.radius_km` is `FORAY_HOME__RADIUS_KM` and `observability.writer_cap` is
  `FORAY_OBSERVABILITY__WRITER_CAP`.
- Settings are read from the process environment and then from a `.env` file in the working
  directory. The environment wins. Unknown `FORAY_*` variables are ignored, so a misspelled name
  fails **silently**: check the spelling against the tables below.
- List and map settings are JSON: `FORAY_COVERAGE='[{"name":"Oregon","place_id":10,"bbox":[-124.6,41.9,-116.4,46.3]}]'`.
- `just` exports the `PG*` variables for local development and puts the nvm Node on `PATH`, so you
  normally set nothing by hand. Run `just` with no arguments to list the recipes.

## Home location and search

| Variable | Default | Meaning |
|---|---|---|
| `FORAY_HOME__NAME` | `Home` | Display name of the default home. |
| `FORAY_HOME__LAT`, `FORAY_HOME__LNG` | `47.6062`, `-122.3321` (Seattle) | The default home, used by anyone who has not set their own. |
| `FORAY_HOME__RADIUS_KM` | `150` | Default search radius (`0 < r <= 20000`). |
| `FORAY_H3_RESOLUTION` | `4` | H3 resolution of a region cell, `0` to `9`. 4 is about 26 km edge length. Changing it requires `foray refresh` to rebuild `phenology` and `regions`. Capped at 9 because the radius-to-cells expansion grows quickly. |

A visitor's own home (set from the search bar) is stored in Postgres per device and overrides these
defaults; see [api.md](api.md#anonymous-device-identity).

## Ingest

| Variable | Default | Meaning |
|---|---|---|
| `FORAY_INGEST__SINCE_YEAR` | `2015` | How far back a home-radius ingest pulls. |
| `FORAY_INGEST__QUALITY_GRADE` | `research` | iNaturalist quality filter at fetch time (`research`, `needs_id`, `casual`). Scoring itself only ever counts `research`. |
| `FORAY_INGEST__RECENT_WEEKS` | `4` | The trailing window behind "Active now" and the recency boost. |
| `FORAY_INGEST__REGION_SYNC_DAYS` | `30` | How far back a coverage-region ingest looks even on its first run. Deep history comes from the bulk load, not this path. |
| `FORAY_COVERAGE` | all 50 US states | JSON list of `{name, place_id, bbox}`. `place_id` is the iNaturalist place; `bbox` is `[west, south, east, north]`, used by the land and trail ingests. Alaska's box is clamped at -180 to avoid crossing the antimeridian. |
| `FORAY_SCOPE_ROOTS` | `[47170]` (Fungi) | JSON list of the root taxon ids whose descendants are ingested, cataloged and ranked. Ingest, the bulk loaders and `revalidate` test "still under a scope root" instead of "still Fungi". A root need not be a kingdom: curated orders or families inside one work too (how trees would be added). |
| `FORAY_SCOPE_KINGDOMS` | `["Fungi"]` | JSON list of kingdom **names** the bulk observation stager pre-filters on (the export carries lineage names, and the stager has no database). Every scope root must sit inside one of them; the loader then applies the exact root test through `taxa`. |
| `FORAY_COUNTRIES` | United States | JSON list of `{name, place_id}`; one observation query per country, which is what the daily job runs. Adding a country is one more entry, no code change. |
| `RIDB_API_KEY` | unset | Recreation.gov key. Without it the live campground ingest is a no-op (the bulk RIDB export needs no key). Also hides the `camps` layer from `/healthz/data`. |
| `FORAY_OVERPASS_URLS` | public mirrors | Comma-separated Overpass endpoints, tried in order. |
| `FORAY_DEM_CACHE` | `~/.cache/foray/dem` | Where `backfill-elevation-dem` keeps downloaded elevation tiles. |

The old per-job cadence variables (`FORAY_INGEST_INTERVAL_HOURS`, `FORAY_RESYNC_BATCH_SIZE`, and
similar) no longer exist: cadences live in [`jobs.yaml`](../jobs.yaml), see [jobs.md](jobs.md).

## Map and tiles

These reach the browser through `GET /api/config`.

| Variable | Default | Meaning |
|---|---|---|
| `FORAY_BASEMAP_URL` | empty | URL of the Protomaps **PMTiles** archive for the base map. Empty means the map shows overlays but no base layer; there is no raster fallback. |
| `FORAY_TERRAIN_URL` | AWS Open Data Terrarium tiles | `{z}/{x}/{y}` template for the hillshade and contour lines. Set it empty to drop the terrain layer. |
| `FORAY_SATELLITE_TILES_URL` | `/api/tiles/satellite/{z}/{x}/{y}.jpg` | Same-origin template for the full-map satellite basemap. Empty disables the toggle. |
| `FORAY_MARTIN_URL` | empty | Internal base URL of the martin tile server, for example `http://martin:3000`. Empty disables the three vector-tile layers (trails, land, fire). `docker-compose.yml` defaults it to the compose service. |
| `FORAY_TRAILS_TILES_URL`, `FORAY_LAND_TILES_URL`, `FORAY_FIRE_TILES_URL` | `/api/tiles/<layer>/{z}/{x}/{y}.pbf` | Same-origin templates sent to the browser when `FORAY_MARTIN_URL` is set. |

The CSP header is built from `FORAY_BASEMAP_URL` and `FORAY_TERRAIN_URL`, so a new tile host works
without editing code; anything else a page loads has to be added in
[`api/security.py`](../src/foray/api/security.py).

## Object storage (bulk snapshots and cached imagery)

Optional, but required for any `ingest-bulk`/`stage-snapshot` command. All are `FORAY_SPACES__*`.

| Variable | Default | Meaning |
|---|---|---|
| `FORAY_SPACES__ACCESS_KEY_ID`, `FORAY_SPACES__SECRET_ACCESS_KEY` | empty | DigitalOcean Spaces (S3-compatible) keys. |
| `FORAY_SPACES__BUCKET` | empty | Bucket name. Configured means all three of key, secret and bucket are set. |
| `FORAY_SPACES__REGION` | `nyc3` | Region of the bucket (production deploys set the droplet's region). |
| `FORAY_SPACES__PUBLIC_URL` | derived | Base URL objects are read back through, such as a CDN endpoint. |

Unconfigured is a supported state: destination imagery falls back to storing bytes in Postgres, and
the bulk commands refuse to run with a clear error.

## Scheduling, health and observability

| Variable | Default | Meaning |
|---|---|---|
| `FORAY_INTERVALS__INGEST_HOURS` | `24` | Expected observation cadence, for `/healthz/data` only. |
| `FORAY_INTERVALS__LAYERS_HOURS` | `168` | Expected land/trails/dispersed/camps cadence. |
| `FORAY_INTERVALS__PRECIP_HOURS`, `FORAY_INTERVALS__FIRE_HOURS` | `24`, `24` | Expected rain and fire cadences. |
| `FORAY_OBSERVABILITY__DATA_FRESHNESS_MULTIPLIER` | `2.0` | How many intervals late a layer may be before `/healthz/data` calls it stale. |
| `FORAY_OBSERVABILITY__WRITER_CAP` | `2` | Max concurrent database-writing jobs. |
| `FORAY_OBSERVABILITY__PHENOLOGY_REBUILD_THRESHOLD` | `50` | Changed rows a job must accumulate before the phenology rebuild actually runs. |
| `FORAY_OBSERVABILITY__RANKING_CACHE_TTL_SECONDS` | `600` | Lifetime of a cached ranked list. |
| `FORAY_OBSERVABILITY__HEALTHCHECKS_URLS` | `{}` | JSON map of job name to ping URL. |
| `FORAY_OBSERVABILITY__NTFY_URL` | empty | ntfy topic for `foray alert`. |
| `FORAY_OBSERVABILITY__SENTRY_DSN` | empty | Sentry/GlitchTip DSN. |
| `FORAY_OBSERVABILITY__LOG_JSON` | `false` | JSON log lines. |
| `FORAY_LOG_LEVEL` | `INFO` | Python log level. |

The `FORAY_INTERVALS__*` values are **not** read from `jobs.yaml`; keep them in step with it, see
[jobs.md](jobs.md#observability).

## Database

The connection is the standard libpq set, read natively by psycopg. There is no DSN setting and no
default password.

| Variable | Meaning |
|---|---|
| `PGHOST`, `PGPORT` | Server address (the local compose database listens on 5432). |
| `PGUSER`, `PGPASSWORD`, `PGDATABASE` | Credentials and database name. |

The test suite uses a separate `foray_test` database so `just test` never wipes your local data.
The server needs PostGIS, h3, pg_trgm and pg_prewarm; see
[architecture.md](architecture.md#data-model).

## Local development files

| File | Purpose |
|---|---|
| `.env` | Your local overrides and secrets (`RIDB_API_KEY`, `FORAY_BASEMAP_URL`, `FORAY_SPACES__*`). Gitignored. |
| `docker-compose.yml` | Postgres (PostGIS + h3), the app, martin, and the optional `scheduler` profile. Reads `.env` for `POSTGRES_PASSWORD`, `RIDB_API_KEY`, `FORAY_BASEMAP_URL`, `DO_SPACES_KEY`, `DO_SPACES_SECRET`, `FORAY_SPACES_BUCKET`, `FORAY_SPACES_REGION` and `FORAY_MARTIN_URL`. |
| `justfile` | Exports `PG*` for host-side commands. |
| `frontend/.nvmrc` | The Node major version the client builds with. |

## Production

The deploy renders `/opt/foray-planner/foray.env` from Ansible variables
([`infra/ansible/templates/foray.env.j2`](../infra/ansible/templates/foray.env.j2)): the `PG*`
connection to the managed cluster, `RIDB_API_KEY`, the default home, `FORAY_BASEMAP_URL`,
`FORAY_TERRAIN_URL`, `FORAY_MARTIN_URL` and `FORAY_SPACES__*`. Both the API container and every
scheduled-job container load the same file. Ansible-side variables (the `foray_*` names) are
documented in [`infra/ansible/AGENTS.md`](../infra/ansible/AGENTS.md) and
[deployment.md](deployment.md).
