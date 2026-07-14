# Deployment

Production target: **Digital Ocean Droplet + Managed Postgres**, deployed via Ansible,
with Cloudflare in front.
The CD pipeline builds and publishes the image on every push to `main`.

---

## CD pipeline (already live)

Every push to `main` triggers `.github/workflows/cd.yml`, which builds a multi-arch image
(`linux/amd64` + `linux/arm64`) with Docker Buildx and pushes it to GHCR:

```
ghcr.io/jahrik/foray-planner:latest
```

The package is public and linked to the repo. No manual build step needed for deployment.

---

## Running with Docker

The container needs a reachable Postgres+PostGIS instance - connection info comes from the
standard libpq env vars (`PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE`), never a config
file or a baked-in default. `foray.cache.SCHEMA` (extension + tables) is applied automatically
on startup, so there's no separate migration step.

```bash
# Pull the latest image
docker pull ghcr.io/jahrik/foray-planner:latest

PG_ENV="-e PGHOST=... -e PGPORT=5432 -e PGUSER=... -e PGPASSWORD=... -e PGDATABASE=foray"

# One-off: initial data ingest (takes a few minutes)
docker run --rm $PG_ENV \
  ghcr.io/jahrik/foray-planner:latest \
  foray ingest --all-regions

# Start the server
docker run -d --name foray-planner -p 8000:8000 $PG_ENV -e RIDB_API_KEY=$RIDB_API_KEY \
  ghcr.io/jahrik/foray-planner:latest
```

The image exposes port `8000` and serves both the API and the built frontend bundle.
The health check polls `GET /api/config` every 30 seconds.

---

## docker-compose stack (local dev)

`make start` brings up two services:

| Service | Role |
|---|---|
| `postgres` | PostGIS 16, health-checked |
| `app` | FastAPI server on port 8000 |

The **scheduler** is behind a docker-compose profile and only starts on demand:

```bash
make scheduler          # starts the background ingest/refresh loop
```

| Service | Role |
|---|---|
| `scheduler` | Background loop: observation ingest every 24h, layers refresh every 168h |

The scheduler runs `scripts/scheduler.sh` which calls `foray ingest --all-regions` and
`foray refresh --with camps,land,dispersed,trails` on configurable intervals. Both intervals
are set via env vars (`FORAY_INGEST_INTERVAL_HOURS`, `FORAY_LAYERS_INTERVAL_HOURS`).

---

## Data refresh in production

The data pipeline is fully decoupled from search. Search/scoring is read-only against cached
data and never triggers network calls. Data stays fresh via:

**Option A - Cron (production default)**

The Ansible playbook configures host cron jobs that run one-off containers against the
managed Postgres instance. Observations ingest daily (04:00 UTC), layers refresh weekly
(Sunday 03:00 UTC). Same image, same DB, spins up, runs, exits.

**Option B - UI Refresh button**

The "Refresh data" button in the UI triggers an in-process refresh for the current home radius.
Runs in a background thread; progress streams via SSE.

**Option C - One-off CLI**

```bash
docker run --rm $PG_ENV \
  ghcr.io/jahrik/foray-planner:latest \
  foray ingest --all-regions
```

Runs as a one-off against the same Postgres instance the live server is using; safe to run
concurrently (Postgres MVCC handles read/write isolation).

---

## Changing location in production

Use the UI's **Set location** bar. It posts to `/api/location`, which upserts the override into
the `app_location` table (see `docs/development.md`) and immediately runs scoring against
existing cached data. No shell access or container restart needed. The override survives
restarts.

If the new area has no data, use the Refresh button or wait for the scheduler to pick it up
on its next cycle.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` | Yes | Postgres connection - read natively by `psycopg`/libpq, never a config file key. |
| `RIDB_API_KEY` | No | Recreation.gov API key for campground ingest. Absent = camps step is a no-op. |
| `FORAY_HOME__LAT` / `FORAY_HOME__LNG` / `FORAY_HOME__RADIUS_KM` | No | Default home location (overridden by `app_location` table if set via UI). |
| `FORAY_COVERAGE` | No | JSON array of `{name, place_id}` for state-level ingest regions. Defaults to WA/OR/ID. |
| `FORAY_INGEST_INTERVAL_HOURS` | No | Scheduler: hours between observation ingests (default: 24). |
| `FORAY_LAYERS_INTERVAL_HOURS` | No | Scheduler: hours between layer refreshes (default: 168). |

Secrets go in the instance environment or a gitignored `.env` file locally.
**Never commit them.**

---

## Production deploy (Digital Ocean + Ansible)

Infrastructure lives in `infra/ansible/`. The playbook provisions a DO Droplet (Docker
pre-installed) and a managed Postgres cluster, then deploys the GHCR container with cron
jobs for data refresh.

### Prerequisites

- Digital Ocean API token (export as `DO_API_TOKEN`)
- SSH key registered in DO (export name as `FORAY_SSH_KEY_NAME`)
- SSH allowlist CIDR (export as `FORAY_SSH_ALLOWED_IPS`, e.g. your IP/VPN range)
- Optional: `RIDB_API_KEY` for campground data, `GHCR_TOKEN` for private images

### First deploy

```bash
export DO_API_TOKEN=dop_v1_...
export FORAY_SSH_KEY_NAME=my-key
export FORAY_SSH_ALLOWED_IPS=203.0.113.0/24
make ansible-install
make ansible-provision
# Note the droplet IP from provision output, then deploy:
export FORAY_DROPLET_IP=<droplet-ip>
make ansible-deploy
```

### Subsequent deploys (app update only)

```bash
export FORAY_DROPLET_IP=<droplet-ip>
make ansible-deploy
```

### Tags

| Tag | Scope |
|---|---|
| `foray` | Everything |
| `foray:provision` | DO resources (Droplet, database, firewall) |
| `foray:deploy` | Pull image, restart container |
| `foray:cron` | Update cron schedules |

---

## Image details

The Dockerfile uses a three-stage build:

| Stage | Base | What it does |
|---|---|---|
| `frontend` | `node:22-slim` | `npm ci` + `npm run build` -> emits the Vite/TS bundle |
| `builder` | `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` | `uv sync --frozen --no-dev` -> self-contained `.venv` |
| `runtime` | `python:3.13-slim-bookworm` | Copies app + venv + bundle; runs as non-root `foray` user |

No local volume - the database is Postgres, reached entirely via env vars.
Port: `8000`.
Default command: `foray serve --host 0.0.0.0 --port 8000`.
