# Development guide

How to set up a working copy, run it, test it and change it. For how the system fits together read
[architecture.md](architecture.md) first; for the reasons behind the conventions, see
[`AGENTS.md`](../AGENTS.md) at the repository root.

Contents: [prerequisites](#prerequisites) | [first run](#first-run) | [daily loop](#the-daily-loop) |
[repo map](#repository-map) | [frontend](#frontend-development) | [tests](#tests) |
[quality gates](#quality-gates) | [the dev database](#the-dev-database) |
[containers](#containers) | [docs](#working-on-the-docs) | [contributing](#contributing) |
[troubleshooting](#troubleshooting)

---

## Prerequisites

| Tool | Why | Notes |
|---|---|---|
| **Python 3.13+** and [uv](https://docs.astral.sh/uv/) | Backend, CLI, tests | uv manages the virtualenv from `uv.lock`; never `pip install` into it |
| [just](https://github.com/casey/just) | Every common task | `uv tool install rust-just` |
| **Node** via [nvm](https://github.com/nvm-sh/nvm) | Frontend build | Not on `PATH` by default; `frontend/.nvmrc` pins the major (currently 24) and the `justfile` puts the matching nvm install on `PATH` |
| **Docker or Podman** with compose | Local Postgres (PostGIS + h3), the app and the tile server | The compose file builds the database image itself |
| `ffmpeg` | Only to re-record the tutorial GIFs | See [tutorials/README.md](tutorials/README.md#updating-these-tutorials) |
| `RIDB_API_KEY` *(optional)* | Live campground ingest | Free from [Recreation.gov](https://ridb.recreation.gov/landing). Without it that step is a no-op and everything else works |

## First run

```bash
uv tool install rust-just   # once
just install                # uv sync + frontend npm ci
just db                     # Postgres in a container; waits until it accepts connections

echo "RIDB_API_KEY=your_key_here" > .env   # optional; .env is gitignored

just ingest                 # nationwide iNaturalist pull + phenology build
just start                  # builds and starts app + postgres + tile server: http://localhost:8000
just scheduler              # optional: the background job loop (see jobs.md)
```

`just ingest` runs `foray ingest --countries`, an **incremental** pull (the last 30 days by default).
For a useful local dataset, either let it run for a while, load history with the bulk path
(`just bulk-stage inat` and `just bulk-load inat`, which need object-storage credentials, see
[configuration.md](configuration.md)), or ingest just your area:
`docker compose run --rm app foray ingest` (the home radius) then `just genera-refresh`. The Genera
pill needs the genus catalog, so run `just genera-refresh` once.

The `justfile` exports `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` (defaulting to the compose
database) and the nvm Node path, so you never set them by hand. `just` with no arguments lists every
recipe, grouped. All settings are documented in [configuration.md](configuration.md).

## The daily loop

```bash
just db && uv run foray serve          # API on :8000 (reads src/foray/web/dist, if built)
cd frontend && npm run dev             # Vite on :5173, proxies /api/* to :8000

just test tests/scoring -k trend       # a subset while iterating
just lint                              # ruff format --check, ruff check, ty, vulture
just frontend                          # eslint + prettier + vitest + tsc + vite build
just check                             # lint + the whole test suite: the CI gate for the backend
```

After changing any `/api/*` route or response model, regenerate the client types and commit them:

```bash
cd frontend && npm run gen:api         # updates src/api/schema.ts and openapi.json
```

CI runs `just check-api-schema` and fails on a difference. After editing code that runs in a
container (`just start`), use `just restart` rather than `start`: a full teardown and rebuild
sidesteps compose's trouble recreating one container of a pod.

## Repository map

See [architecture.md](architecture.md#repository-layout) for the package-by-package tour. The short
version:

```
src/foray/        the Python package (api/, cache/, scoring/, sources/, cli.py, ...)
frontend/         the TypeScript client (see frontend.md)
tests/            pytest, mirroring src/foray (tests/scoring, tests/sources, ...)
docs/             these docs; docs/tutorials/ holds the illustrated user guides + recorder
infra/ansible/    production provisioning and deploy      infra/docker/postgres  dev database image
jobs.yaml         scheduled jobs (see jobs.md)            justfile + just/       task runner
.github/          ci.yml, cd.yml, bulk-load.yml, dependabot
```

---

## Frontend development

Covered in [frontend.md](frontend.md#running-and-building): run the backend and `npm run dev` side by
side. The map needs a `basemap_url` (`FORAY_BASEMAP_URL`) to show a base layer; copy the production
archive URL into `.env` to see real cartography locally.

## Tests

```bash
just test                                  # everything (starts Postgres)
just test tests/scoring/test_scoring.py   # one file
just test tests/scoring/test_scoring.py::test_april_ranks_morel_region_first
just test -k haversine                     # by keyword
```

- **Hermetic.** No test touches the network. Geocoding and HTTP are mocked with
  `httpx.MockTransport`; scoring uses fixtures. The only external dependency is the local Postgres.
- **Real database.** Tests run against a `foray_test` database (never your dev data) with PostGIS and
  h3, using a shared connection and `TRUNCATE` before each test rather than a transaction per test;
  see `tests/conftest.py` for why.
- **Coverage gate.** The full run enforces a ratchet-only floor (78% at the time of writing, in
  `pyproject.toml`); raise it as coverage improves and never lower it. `just test <args>` relaxes the
  gate because a subset can never meet it.
- **Layout.** `tests/test_*.py` for the top-level modules, `tests/scoring/` and `tests/sources/` for
  the packages. A new source module gets a `tests/sources/test_<name>.py`.
- **Frontend.** Vitest with jsdom, one `*.test.ts` beside each module; see
  [frontend.md](frontend.md#testing).
- **Migrations.** CI also applies the entire migration chain to a fresh Postgres in its own job, so a
  migration that only works against an existing database fails there.

## Quality gates

`pre-commit` runs the fast checks on commit and the heavy ones (`just test`, `just frontend`) on
`git push`:

```bash
uv run pre-commit install --hook-type pre-commit --hook-type pre-push   # once
```

| Check | Where |
|---|---|
| ruff format, ruff check (E, F, I, UP, B, SIM, C4, RUF, PERF, PIE, PTH; line length 120) | `just lint`, commit hook |
| ty (type checking) | `just lint`, commit hook |
| vulture (dead code, confidence 80) | `just lint`, commit hook |
| pytest with coverage floor | `just test`, push hook, CI |
| eslint, prettier, vitest, `tsc`, `vite build` | `just frontend`, push hook, CI |
| `check-added-large-files` (512 KB), except the tutorial media | commit hook |
| hadolint | The Dockerfile (CI). Ansible changes: `just ansible lint` (yamllint + ansible-lint) and molecule, see [`infra/ansible/AGENTS.md`](../infra/ansible/AGENTS.md) |

Run `just check` (and `just frontend` when you touched `frontend/`) before you push, so CI does not
find it for you. Style rules the linters cannot enforce: **no single-letter variable names** (any
language), no new dependency without a reason, and comments that explain *why*.

## The dev database

The compose `postgres` service is PostGIS on Postgres 17 plus the h3 extension
([`infra/docker/postgres/Dockerfile`](../infra/docker/postgres/Dockerfile)), published on port 5432
with a named volume `foray-postgres-data`. The schema and migrations are applied automatically on
first connect and on app start, and you can apply them explicitly with `uv run foray migrate`.

- **Reset everything:** `just clean` removes containers *and* the volume.
- **Look around:** `just psql "SELECT count(*) FROM observations"` (no local `psql` needed).
- **Rebuild derived tables:** `uv run foray refresh --with mushrooms` rebuilds `phenology` and
  `regions`. Changing `FORAY_H3_RESOLUTION` requires it.
- **Empty trails, land, fire or camps near a pin?** Make sure the table's `geom` column is populated.
  Every "near" query silently returns nothing when `geom IS NULL`, which can happen in a database that
  predates the PostGIS migration; `scripts/backfill_geom.py` repairs it.
- **Upgrading from an older Postgres volume** fails to start (a data directory is tied to its major
  version): `just clean` and re-ingest.

## Containers

```bash
docker build -t local/foray-planner:dev .
```

The Dockerfile has three stages: the frontend build (`node:26-slim`), the Python build
(`uv` on `python3.13-bookworm-slim`, producing a self-contained `.venv`) and a lean runtime
(`python:3.13-slim-bookworm` plus `libexpat1` and `gdal-bin`, running as uid 1000, no volume). The same
image runs the web server, every scheduled job and the bulk stagers. Production details are in
[deployment.md](deployment.md).

## Working on the docs

Documentation is part of the change. In a pull request that alters behavior:

- Update the page that describes it. [docs/README.md](README.md) maps topics to pages.
- Route or model change: run `npm run gen:api`, and update [api.md](api.md).
- Scoring weight or formula: update the constant **and** [scoring.md](scoring.md).
- New or changed job: edit `jobs.yaml` and [jobs.md](jobs.md); new CLI command or option:
  [cli.md](cli.md); new setting: [configuration.md](configuration.md).
- New external dataset: a section in [data-sources.md](data-sources.md) with its licence, limits and
  attribution, and the credit in the map's attribution string.
- A visible UI change: re-record the affected tutorial (`just tutorials <name>`) and look at the
  result before committing it.

House style: plain, specific prose; hyphens or colons instead of em dashes; no secrets, API keys,
IP addresses or internal hostnames in any file; **no identification, edibility or safety claims**.
Check that links resolve and that every command you quote runs.

## Contributing

1. Branch from `main`; never commit to `main` directly.
2. Keep one issue to one pull request, with every part of the issue in it.
3. Run `just check` (and `just frontend`) locally.
4. Open a pull request that links the issue and says what you verified. CI must pass; a merge to
   `main` deploys to production, so a docs-only change that should not redeploy can say
   `[skip deploy]` in its head commit message.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `psycopg.OperationalError` connecting | `just db` first; check `PGHOST`/`PGPORT` if you override them |
| `extension "h3" is not available` | You are on a plain `postgis/postgis` image. Use the compose database (`just db`), which adds h3 |
| The app says "no data for this area yet" | Nothing ingested for the area: `just ingest`, then `just genera-refresh` |
| The map has no base layer | Set `FORAY_BASEMAP_URL` (see [configuration.md](configuration.md#map-and-tiles)) |
| Trails, land and fire missing but the rest works | The martin tile server is not running or `FORAY_MARTIN_URL` is empty; `just start` brings it up |
| UI looks stale after a rebuild | Hard-reload; if needed unregister the service worker (browser devtools, Application tab) |
| `npm` or `node` not found | Install the nvm Node named in `frontend/.nvmrc`; `just` finds it under `~/.nvm` |
| Compose recreates one container badly (podman) | `just restart` (full down then up) |
