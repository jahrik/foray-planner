# Deployment

Production target: **AWS ECS Fargate + RDS Postgres** via CDK, with Cloudflare in front (the
`infra/cdk/` app + the full rewrite of the setup steps below land in a follow-up PR - this one
covers the Postgres/PostGIS migration the deploy depends on). The CD pipeline already builds and
publishes the image on every push to `main`.

> The "Planned Lightsail setup" section below is stale (superseded by the CDK/ECS/RDS direction
> above) and will be replaced wholesale in the infra PR - the DuckDB-specific details in it
> (spatial extension RAM sizing, `/data` volume, `location.json`) no longer apply after this
> migration either way.

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

# One-off: initial data refresh (takes a few minutes)
docker run --rm $PG_ENV -e RIDB_API_KEY=$RIDB_API_KEY \
  ghcr.io/jahrik/foray-planner:latest \
  foray --config config.docker.yaml refresh

# Start the server
docker run -d --name foray-planner -p 8000:8000 $PG_ENV -e RIDB_API_KEY=$RIDB_API_KEY \
  ghcr.io/jahrik/foray-planner:latest
```

The image exposes port `8000` and serves both the API and the built frontend bundle.
The health check polls `GET /api/config` every 30 seconds.

---

## Refreshing data in production

Postgres has no DuckDB-style single-writer-file constraint, so a standalone `foray refresh`
process can run **concurrently** with the live server against the same database - no
stop/restart dance needed.

**Option A - UI Refresh button (recommended for normal use)**

The "Refresh data" button in the UI triggers an in-process refresh. Always safe, no shell
access needed.

**Option B - Standalone `foray refresh`**

```bash
docker run --rm $PG_ENV -e RIDB_API_KEY=$RIDB_API_KEY \
  ghcr.io/jahrik/foray-planner:latest \
  foray --config config.docker.yaml refresh
```

Runs as a one-off against the same Postgres instance the live server is using; no need to stop
the server first.

---

## Changing location in production

Use the UI's **Set location** bar. It posts to `/api/location`, which upserts the override into
the `app_location` table (see `docs/development.md`) and triggers an in-process refresh
automatically. No shell access or container restart needed - and the override survives restarts,
unlike the old file-based approach.

---

## Planned Lightsail setup

1. **Create a Lightsail Linux instance** - start at 1 GB RAM; the DuckDB spatial extension
   needs ~512 MB during dispersed-camping ingest. Size up to 2 GB if refresh is slow.
2. **Attach a persistent disk** - mount at `/data`. The DuckDB cache survives instance
   restarts and is the only stateful piece.
3. **Install Docker** on the instance.
4. **Pull and run** the GHCR image as shown above.
5. **Set `RIDB_API_KEY`** as an instance environment variable - never committed to the repo.
6. **Cloudflare in front:**
   - Proxy DNS + TLS termination
   - Cloudflare Access (email or Google auth) = private app with no app-level auth code
   - Static IP on the Lightsail instance so the DNS record stays stable

### Planned architecture

```
Browser → Cloudflare (TLS + Access) → Lightsail static IP
                                              │
                                    Docker: foray-planner
                                              │
                                    /data (persistent disk)
                                     └─ foray.duckdb
                                     └─ location.json
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` | Yes | Postgres connection - read natively by `psycopg`/libpq, never a config file key. |
| `RIDB_API_KEY` | No | Recreation.gov API key for campground ingest. Absent = camps step is a no-op. |

Secrets go in the instance environment, Secrets Manager, or a gitignored `.env` file locally.
**Never commit them.**

---

## Image details

The Dockerfile uses a three-stage build:

| Stage | Base | What it does |
|---|---|---|
| `frontend` | `node:22-slim` | `npm ci` + `npm run build` → emits the Vite/TS bundle |
| `builder` | `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` | `uv sync --frozen --no-dev` → self-contained `.venv` |
| `runtime` | `python:3.13-slim-bookworm` | Copies app + venv + bundle; runs as non-root `foray` user |

No local volume - the database is Postgres, reached entirely via env vars.
Port: `8000`.
Default command: `foray --config config.docker.yaml serve --host 0.0.0.0 --port 8000`.
