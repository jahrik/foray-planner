# Deployment

Planned production target: **AWS Lightsail** with Cloudflare in front.
The CD pipeline already builds and publishes the image on every push to `main`.

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

```bash
# Pull the latest image
docker pull ghcr.io/jahrik/foray-planner:latest

# Create a persistent volume for the DuckDB cache + runtime location.json
docker volume create foray-data

# One-off: initial data refresh (populates /data/foray.duckdb — takes a few minutes)
docker run --rm \
  -v foray-data:/data \
  -e RIDB_API_KEY=$RIDB_API_KEY \
  ghcr.io/jahrik/foray-planner:latest \
  foray --config config.docker.yaml refresh

# Start the server
docker run -d \
  --name foray-planner \
  -p 8000:8000 \
  -v foray-data:/data \
  -e RIDB_API_KEY=$RIDB_API_KEY \
  ghcr.io/jahrik/foray-planner:latest
```

The image exposes port `8000` and serves both the API and the built frontend bundle.
The health check polls `GET /api/config` every 30 seconds.

---

## Refreshing data in production

> **DuckDB single-writer constraint:** DuckDB allows only one read-write connection per file
> per process. The running server holds that connection. A separate `foray refresh` process
> cannot open the same file read-write simultaneously — it will get a lock error.

### Safe patterns

**Option A — UI Refresh button (recommended for normal use)**

The "Refresh data" button in the UI triggers an in-process refresh (same DuckDB connection,
same process). This is always safe while the server is running and requires no shell access.

**Option B — Stop → refresh → restart**

```bash
docker stop foray-planner
docker run --rm \
  -v foray-data:/data \
  -e RIDB_API_KEY=$RIDB_API_KEY \
  ghcr.io/jahrik/foray-planner:latest \
  foray --config config.docker.yaml refresh
docker start foray-planner
```

**Option C — Snapshot-and-swap (future)**

The refresh worker writes to `foray.build.duckdb`, then atomically renames it over the live
file. The server reopens the file after the swap. This isolates the writer and produces a
clean snapshot that can double as the offline field export. Tracked in `TODO.md`.

---

## Changing location in production

Use the UI's **Set location** bar. It posts to `/api/location`, saves `data/location.json`
to the `/data` volume, and triggers an in-process refresh automatically. No shell access
or container restart needed.

---

## Planned Lightsail setup

1. **Create a Lightsail Linux instance** — start at 1 GB RAM; the DuckDB spatial extension
   needs ~512 MB during dispersed-camping ingest. Size up to 2 GB if refresh is slow.
2. **Attach a persistent disk** — mount at `/data`. The DuckDB cache survives instance
   restarts and is the only stateful piece.
3. **Install Docker** on the instance.
4. **Pull and run** the GHCR image as shown above.
5. **Set `RIDB_API_KEY`** as an instance environment variable — never committed to the repo.
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
| `RIDB_API_KEY` | No | Recreation.gov API key for campground ingest. Absent = camps step is a no-op. |

Secrets go in the instance environment or a gitignored `.env` file. **Never commit them.**

---

## Image details

The Dockerfile uses a three-stage build:

| Stage | Base | What it does |
|---|---|---|
| `frontend` | `node:22-slim` | `npm ci` + `npm run build` → emits the Vite/TS bundle |
| `builder` | `ghcr.io/astral-sh/uv:python3.13-bookworm-slim` | `uv sync --frozen --no-dev` → self-contained `.venv` |
| `runtime` | `python:3.13-slim-bookworm` | Copies app + venv + bundle; runs as non-root `foray` user |

Volume: `/data` — DuckDB cache + runtime `location.json`.
Port: `8000`.
Default command: `foray --config config.docker.yaml serve --host 0.0.0.0 --port 8000`.
