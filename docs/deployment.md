# Deployment

Production target: **AWS Lightsail** with Cloudflare in front. The CD pipeline already builds
and publishes the image on every push to `main`; `infra/` scripts the Lightsail side (see
below). Provisioning itself is a deliberate, billed, one-off action - not run automatically
by CI - so it's a manual `./infra/deploy.sh` invocation, not a workflow.

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

# One-off: initial data refresh (populates /data/foray.duckdb - takes a few minutes)
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
> cannot open the same file read-write simultaneously - it will get a lock error.

### Safe patterns

**Option A - UI Refresh button (recommended for normal use)**

The "Refresh data" button in the UI triggers an in-process refresh (same DuckDB connection,
same process). This is always safe while the server is running and requires no shell access.

**Option B - Stop → refresh → restart**

```bash
docker stop foray-planner
docker run --rm \
  -v foray-data:/data \
  -e RIDB_API_KEY=$RIDB_API_KEY \
  ghcr.io/jahrik/foray-planner:latest \
  foray --config config.docker.yaml refresh
docker start foray-planner
```

**Option C - Snapshot-and-swap (future)**

The refresh worker writes to `foray.build.duckdb`, then atomically renames it over the live
file. The server reopens the file after the swap. This isolates the writer and produces a
clean snapshot that can double as the offline field export. Tracked in `TODO.md`.

---

## Changing location in production

Use the UI's **Set location** bar. It posts to `/api/location`, saves `data/location.json`
to the `/data` volume, and triggers an in-process refresh automatically. No shell access
or container restart needed.

---

## Lightsail setup (`infra/`)

`infra/deploy.sh` scripts the AWS side end to end and is idempotent (safe to re-run):

```bash
cd infra
./deploy.sh          # override REGION, BUNDLE_ID, etc. via env vars - see the script header
```

It creates (or reuses, if already present) a Lightsail instance, a static IP, and a
persistent block-storage disk attached at `/dev/xvdf`; `infra/cloud-init.yaml` runs on first
boot to install Docker, partition + mount that disk at `/data`, and install (but not yet
start) the `foray-planner` systemd unit (`infra/foray-planner.service`).

The API key is deliberately **not** part of the automated flow - Lightsail/EC2 instance
user-data is visible to anyone with `describe-instance` access on the account, so it's not a
safe place for a real secret. `deploy.sh` prints the exact next steps at the end:

1. SSH in (`ssh ubuntu@<static-ip>`), edit `/etc/foray/env`, set the real `RIDB_API_KEY`.
2. `sudo systemctl start foray-planner` (and `enable`, already done by cloud-init).
3. `curl http://<static-ip>/api/config` to confirm it's serving.

Re-running `deploy.sh` after editing `BUNDLE_ID`/`DISK_SIZE_GB`/etc. only touches what
changed - it checks each resource's existence before creating it.

### Cloudflare in front

- Proxy DNS + TLS termination. `infra/cloudflare-dns.sh` can create/update the proxied `A`
  record via the Cloudflare API (needs a `Zone:DNS:Edit`-scoped token); or do it by hand in
  the dashboard - same result.
- **Cloudflare Access** (Zero Trust -> Access -> Applications): add an application for the
  hostname, policy = your email or Google login. This is what makes the app private with no
  app-level auth code. Dashboard-only step - not scripted (policy choices are a one-time
  judgment call, not worth automating for a single box).
- SSL/TLS mode **Flexible** works out of the box (origin serves plain HTTP on `:80`, exactly
  what `foray-planner.service` publishes). Move to **Full (strict)** later if TLS gets added
  on the box directly.
- Domain via Cloudflare Registrar (or point an existing domain's nameservers at Cloudflare).

### Architecture

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

Volume: `/data` - DuckDB cache + runtime `location.json`.
Port: `8000`.
Default command: `foray --config config.docker.yaml serve --host 0.0.0.0 --port 8000`.
