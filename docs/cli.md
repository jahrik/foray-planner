# Command-line reference

`foray` is the Click CLI defined in [`src/foray/cli.py`](../src/foray/cli.py). Run it with
`uv run foray <command>` on a development machine, or `docker run ... <image> foray <command>` /
`docker compose run --rm app foray <command>` where the app is containerized. `uv run foray --help`
and `uv run foray <command> --help` always show the current options; this page explains what each
command is *for*.

Most day-to-day commands also have a [`just` recipe](#just-recipes) that starts the database and
runs the command in the compose `app` container.

Conventions that hold for nearly every command:

- **Idempotent.** Writes are upserts keyed by the source's own id, so re-running is safe.
- **Bounded.** Long-running commands work in batches and commit as they go, so they are safe to
  interrupt and resume.
- **Config from the environment.** Nothing takes a database address on the command line; see
  [configuration.md](configuration.md).
- **Row counts.** Commands run by the scheduler report how many rows they touched to the job
  wrapper; see [jobs.md](jobs.md).

---

## Run the app

| Command | What it does |
|---|---|
| `foray serve [--host H] [--port P]` | Start the FastAPI app and serve the built client. Runs **one** process on purpose (the ranking cache is in-process). In containers use `--host 0.0.0.0 --port 8000`. |
| `foray openapi` | Print the OpenAPI schema as JSON. `just check-api-schema` and `npm run gen:api` feed this to the frontend type generator. |
| `foray migrate` | Apply the schema and the whole migration chain, then exit. CD runs it once per deploy, before any container using the new image starts. |

## Pull observations

| Command | What it does |
|---|---|
| `foray ingest` | Pull every Fungi observation within the **home radius**. |
| `foray ingest --region NAME` | One named coverage region (a state, from `FORAY_COVERAGE`). |
| `foray ingest --all-regions` | Every coverage region, one at a time. |
| `foray ingest --countries` | One query per configured country. This is what the daily `ingest` job and `just ingest` run; it avoids double-counting near state borders. |
| `foray genera-refresh` | Sync the Fungi genus catalog (and its taxonomy) from iNaturalist into `fungi_genera`. Run once on a fresh database; weekly after that. |

Each pull is recorded in the ingest log, so an area and window already covered is skipped. History
beyond the incremental window arrives through the bulk path, below.

## Pull map layers

| Command | What it does |
|---|---|
| `foray camps` | Developed campgrounds from Recreation.gov within the home radius. Needs `RIDB_API_KEY`; without it, a no-op. Skips itself once a bulk RIDB snapshot has ever loaded. |
| `foray land [--all]` | BLM, USFS, PAD-US and tribal ownership polygons for the home radius, or for the whole coverage envelope in one query with `--all`. |
| `foray dispersed [--all]` | OSM-reported dispersed camping (camp sites, pitches, backcountry tags). `--all` is coverage-wide. |
| `foray trails [--region NAME \| --all] [--force]` | OSM trails, forest roads, hiking routes and trailheads through Overpass. Coverage-wide trails now arrive through the Geofabrik bulk source, so this is a manual tool; `--force` re-fetches regions already ingested at the current query version. |
| `foray fire` | Active wildfire perimeters (replace semantics: a contained fire drops off) and the last three completed fire years of burn scars, from NIFC. |
| `foray refresh [--with LAYERS] [--all]` | The combined sequence: observations, then campgrounds, land, dispersed and trails, then a phenology rebuild. `--with mushrooms,camps,land,dispersed,trails` picks a subset. `--all` runs the region-scoped targets across all coverage instead of just the home radius. This is the same sequence the web **Refresh** button runs. |

## Enrich observations (backfills)

| Command | What it does |
|---|---|
| `foray backfill-elevation [--limit N] [--rebuild/--no-rebuild]` | Elevation from Open-Meteo's DEM for observations without one. Drains until the free tier rate-limits, then stops. Now mostly a safety net. |
| `foray backfill-elevation-dem [--dry-run] [--workers N] [--batch-size N] [--sleep S] [--max-cells N]` | Elevation from local Copernicus GLO-90 tiles, so it is not rate-limited. Writes use a bulk `COPY` plus one `UPDATE ... FROM` per batch. `--dry-run` downloads tiles and reports without writing. |
| `foray backfill-precip [--limit N] [--rebuild/--no-rebuild]` | Rain in the 7 and 30 days before each observation, from the ERA5 archive. A window inside ERA5's 5 to 7 day lag stays empty and is retried. |
| `foray refresh-precip` | Recent rain (trailing 7, 14, 30 days) for every active destination cell. Skips cells refreshed in the last 20 hours, so a re-run resumes. |
| `foray backfill-forage [--limit N]` | Recount research-grade fungi records within about 500 m of each trail (`trails.forage_obs`), stalest first. |
| `foray backfill-trail-land` | One-time: tag existing trails with the public land they cross. New trails are tagged at ingest. |
| `foray backfill-satellite [--limit N] [--concurrency N] [--refresh]` | Pre-fetch the Esri aerial and label rasters for every region so selecting a destination never waits on a live fetch. `--refresh` clears the table first; use it after changing what a region's raster contains. |

## Keep the cache honest

| Command | What it does |
|---|---|
| `foray revalidate` | Re-check observations under genera whose cached count has drifted from iNaturalist's live count; purge or reassign those misfiled under a same-named animal genus. |
| `foray resync [--batch-size N] [--until-done]` | Re-check the whole cache against iNaturalist, oldest-checked first. The only path that verifies every column, including `obscured`. One batch by default (the scheduler runs it hourly); `--until-done` loops until every row has been checked once, for a deliberate catch-up after a data bug. |
| `foray prune-duplicates` | Fold duplicate campsites and trailheads cached before the dedup rules existed, tile by tile. Run it detached on a large database. |

## Bulk snapshots

See [architecture.md](architecture.md#bulk-snapshots) for how the pipeline works.

| Command | What it does |
|---|---|
| `foray stage-snapshot SOURCE` / `--all` | Fetch a source and upload its snapshot to object storage, without touching Postgres. Runs from GitHub Actions weekly. `--all` stages every source in `ingest_bulk.STAGERS`. |
| `foray ingest-bulk SOURCE` | Load the newest staged snapshot into Postgres if it is newer than what was last loaded (tracked in `meta`). A no-op when nothing is new. |

Registered sources: `inat` (full Fungi history), `ridb` (campgrounds), `usfs_trails`, `usfs_mvum`
(Forest Service roads), `osm_trails` (Geofabrik extracts) and `ravg` (burn severity). Both commands
refuse to run unless `FORAY_SPACES__*` is configured.

## Scheduling and operations

| Command | What it does |
|---|---|
| `foray scheduler` | The development loop: read `jobs.yaml` and run each job through `foray job` on its interval, forever. |
| `foray job NAME [--writer] -- COMMAND...` | Run one command with an overlap lock, a `job_runs` row, health pings and failure alerts. Example: `foray job fire -- fire`. See [jobs.md](jobs.md). |
| `foray alert LEVEL MESSAGE` | Log a message and, if `FORAY_OBSERVABILITY__NTFY_URL` is set, push it to that topic. Used by deploy tasks and failing jobs. |

## Plan a trip

```bash
uv run foray plan                                   # best reachable destination from home
uv run foray plan --destination "Bend, OR" --months 9,10 --max-stops 4
```

| Option | Default | Meaning |
|---|---|---|
| `--months` | current month | Comma-separated, 1 to 12. |
| `--destination` | auto-pick | A place name (one Nominatim lookup) or `lat,lng`. |
| `--corridor-km` | 60 | How far off the straight line a stop may be. |
| `--max-stops` | 5 | Stays in the itinerary. |
| `--max-drive-km` | 400 | Longest single leg (great-circle). |
| `--any-camp` | off | Allow stops whose nearest camp is not tagged free. |

Differences from `GET /api/plan`: the CLI starts from the **server default home** (not a visitor's
saved one), considers **all genera**, and requires a free camp unless you pass `--any-camp`
(the API's `require_free_camp` defaults to false). It reads cached data and does no network I/O
unless `--destination` is a place name. See [scoring.md](scoring.md#trip-planner).

---

## `just` recipes

`just` with no arguments lists everything, grouped. The data recipes start Postgres and run the
command in the compose `app` container.

| Group | Recipe | Equivalent |
|---|---|---|
| dev | `just install` | `uv sync` and `npm ci` |
| dev | `just check` | `just lint` then `just test`, the full local CI gate |
| dev | `just lint` | `ruff format --check`, `ruff check`, `ty check`, `vulture` |
| dev | `just test [args]` | `pytest` (starts Postgres; `just test tests/foo.py -k name` for a subset) |
| dev | `just frontend` | ESLint, Vitest, `tsc --noEmit`, `vite build` |
| dev | `just check-api-schema` | Regenerate the client types and fail on a diff |
| dev | `just tutorials [args]` | Re-record the how-to GIFs, see [tutorials/README.md](tutorials/README.md#updating-these-tutorials) |
| dev | `just psql "SQL"` | A one-off query in the Postgres container |
| services | `just db` / `start` / `stop` / `restart` / `clean` / `scheduler` | Compose control: database, app, teardown, rebuild, scheduler profile |
| data | `just ingest` | `foray ingest --countries` |
| data | `just genera-refresh`, `fire`, `refresh-precip`, `backfill-precip`, `backfill-forage`, `backfill-trail-land`, `prune-duplicates`, `revalidate` | The command of the same name |
| data | `just resync [args]`, `just trails [args]` | Pass options through: `just resync "--until-done --batch-size 20000"` |
| data | `just bulk-stage SOURCE`, `just bulk-load SOURCE` | `stage-snapshot` and `ingest-bulk` |
| deps | `just lock`, `just patch` | Re-resolve or upgrade all three lockfiles |
| deploy | `just ansible <recipe>` | Provision, deploy and one-off tasks, see [deployment.md](deployment.md) |
