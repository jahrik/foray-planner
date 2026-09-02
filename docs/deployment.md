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

The package is public and linked to the repo. No manual build step needed to publish it.

Once the image is published, the same workflow's `deploy` job runs `foray:deploy` against
prod automatically - see [CI/CD deploy (automated)](#cicd-deploy-automated) below. Building
and publishing the image is unconditional; deploying it is gated behind manual approval.

---

## Running with Docker

The container needs a reachable Postgres instance - connection info comes from the
standard libpq env vars (`PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE`), never a config
file or a baked-in default. `foray.cache.SCHEMA` (tables) is applied automatically
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

`just start` brings up two services:

| Service | Role |
|---|---|
| `postgres` | Postgres 16, health-checked |
| `app` | FastAPI server on port 8000 |

The **scheduler** is behind a docker-compose profile and only starts on demand:

```bash
just scheduler          # starts the background ingest/refresh loop
```

| Service | Role |
|---|---|
| `scheduler` | Background loop: observation ingest every 24h, layers refresh every 168h, observation revalidate + resync + elevation backfill hourly |

The scheduler runs `scripts/scheduler.sh`, which calls `foray ingest --countries`,
`foray refresh` (layers), `foray revalidate`, `foray resync --batch-size N`, and
`foray backfill-elevation --limit N`, each on its own interval. Set via env vars:
`FORAY_INGEST_INTERVAL_HOURS` (24), `FORAY_LAYERS_INTERVAL_HOURS` (168),
`FORAY_REVALIDATE_INTERVAL_HOURS` (168), `FORAY_RESYNC_INTERVAL_HOURS` (1) +
`FORAY_RESYNC_BATCH_SIZE` (2000), `FORAY_ELEVATION_INTERVAL_HOURS` (1) +
`FORAY_ELEVATION_LIMIT` (20000).

---

## Data refresh in production

The data pipeline is fully decoupled from search. Search/scoring is read-only against cached
data and never triggers network calls. Data stays fresh via:

**Option A - Cron (production default)**

The Ansible playbook configures host cron jobs that run one-off containers against the
managed Postgres instance. Same image, same DB, spins up, runs, exits:

| Job | Schedule (UTC) |
|---|---|
| `foray-ingest` (`foray ingest --countries`) | daily 04:00 |
| `foray-layers` (`foray refresh` layers) | weekly Sun 03:00 |
| `foray-genera` (`foray genera-refresh`) | weekly Sun 02:00 |
| `foray-revalidate` (`foray revalidate`) | weekly Sun 05:00 |
| `foray-resync` (`foray resync --batch-size N`) | hourly :30 |
| `foray-backfill-elevation` (`foray backfill-elevation`, issue #36) | hourly :45 |

The elevation backfill drains the per-observation elevation backlog from Open-Meteo's free
DEM; each run enriches as many rows as the free tier allows before it rate-limits, then exits
(`lookup_batch` backs off on `Retry-After` first). Schedules are Ansible vars
(`foray_*_cron_*` in `infra/ansible/defaults/main.yml`).

Open-Meteo's free tier caps at ~10k points/day, so a fresh multi-million-row backlog would
take ~200 days to clear at that pace. To clear it in one pass instead, run
`just ansible backfill-elevation-dem-once` (tag `foray:backfill-elevation-dem-once`): it
downloads ~5 GB of Copernicus GLO-90 tiles onto the droplet, samples the same DEM locally for
every outstanding row, then removes the tiles. Writes go through a `COPY` into a TEMP table
plus one `UPDATE ... FROM` per batch (`--batch-size`), with `--sleep` pacing, a short
`lock_timeout` (a batch that can't get its lock is left for a re-run), and
`synchronous_commit = off`. The Ansible task passes `--no-rebuild` - like the hourly cron, it
leaves the phenology rematerialize to the next daily ingest, so region elevation means land
within a day. A partial run is safe to repeat: run the target again to finish the rest.
See `scripts/backfill_elevation_dem.py`.

Even set-based, a full ~1.8M-row pass saturates the 1-vCPU managed Postgres enough to starve
the live `/api/destinations` query, so run it inside a **maintenance window**:

```sh
ssh root@<droplet> 'touch /opt/foray-planner/www/maintenance.on'   # Caddy now serves the 503 page
ssh root@<droplet> 'docker stop foray-planner'                     # free the DB of the app's queries
FORAY_DROPLET_IP=<droplet> just ansible backfill-elevation-dem-once
ssh root@<droplet> 'docker start foray-planner'
ssh root@<droplet> 'rm /opt/foray-planner/www/maintenance.on'
```

The next deploy clears `maintenance.on` on its own (see `tasks/deploy/caddy.yml`), so a
forgotten flag heals at the next push.

### Maintenance mode

`touch /opt/foray-planner/www/maintenance.on` on the droplet makes Caddy serve
`www/maintenance.html` (HTTP 503, `Retry-After`) to everyone, whether or not the app is up;
`rm` it to restore. Caddy also serves the same page automatically whenever the backend is
unreachable - a deploy restart, a crash - instead of a bare `502`. The page is
self-contained and refreshes itself every 60s.

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
jobs for data refresh. `inventory/hosts.yml` pins `ansible_python_interpreter` to
`/usr/bin/python3` explicitly (rather than relying on Ansible's interpreter auto-discovery) so
a second Python install on the droplet can't change which interpreter gets *discovered*, and
the discovery warning is silenced. This doesn't lock the Python version itself -
`/usr/bin/python3` is a distro-managed symlink that can still repoint to a different minor
version on an OS/package update.

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
just ansible install
just ansible provision
# Note the droplet IP from provision output, then deploy:
export FORAY_DROPLET_IP=<droplet-ip>
just ansible deploy
```

### Subsequent deploys (app update only)

```bash
export FORAY_DROPLET_IP=<droplet-ip>
just ansible deploy
```

### Tags

| Tag | Scope |
|---|---|
| `foray` | Provision + deploy + cron (not ingest-once - see below) |
| `foray:provision` | DO resources (Droplet, database, firewall) |
| `foray:deploy` | Pull image, restart container |
| `foray:cron` | Update cron schedules |
| `foray:ingest-once` | Manual/opt-in full data ingest (`just ansible ingest-once`) - not part of `foray:deploy` or the `foray` umbrella; the daily `foray-ingest` cron job already keeps data fresh, so this only exists for warming a fresh droplet's data immediately instead of waiting for the next cron run. **Run this only after the first `foray:deploy`** - it depends on the env file that deploy renders (`/opt/foray-planner/foray.env`) and fails fast with a clear message if that hasn't happened yet. |
| `foray:backfill-elevation-dem-once` | Manual/opt-in one-pass elevation backfill (`just ansible backfill-elevation-dem-once`) - samples local Copernicus GLO-90 tiles on the droplet to clear the whole elevation backlog at once, instead of the hourly `foray-backfill-elevation` cron trickling through Open-Meteo's ~10k/day free tier. Downloads ~5 GB of tiles, then removes them. Set-based `COPY` + `UPDATE ... FROM` writes with `--sleep` pacing and a short `lock_timeout` so the live site keeps serving; `--no-rebuild` (daily ingest rematerializes phenology). Safe to re-run to finish a partial pass. Same env-file dependency and fail-fast as `foray:ingest-once`, plus a free-disk precheck. |
| `foray:firewall-allow-runner` / `foray:firewall-revoke-runner` | CI-internal only - adds/removes the GitHub Actions runner's own IP from the live SSH firewall rule around an automated `foray:deploy` run (see below). Not something an operator runs directly. |

---

## CI/CD deploy (automated)

`.github/workflows/cd.yml`'s `deploy` job runs `foray:deploy` against prod automatically
after every image publish on `main`, replacing the need to manually run `just ansible deploy`
for routine app updates. `foray:provision` and the one-off tags stay manual/local - only the
app-update path (`foray:deploy`) is automated.

**Manual approval gate.** The `deploy` job targets a GitHub `production` Environment with
required reviewers, so it pauses after the image is published and waits for someone to
approve it in the Actions tab before touching the droplet - merging to `main` alone never
silently redeploys prod.

**No standing SSH access for CI.** GitHub-hosted runners don't have a stable IP, so the
firewall isn't opened permanently for them. Each deploy run: resolves its own runner's public
IP, adds it to the DO firewall's SSH rule (`foray:firewall-allow-runner`), runs `foray:deploy`
over SSH using a dedicated deploy key (not the operator's personal key), then removes its IP
again (`foray:firewall-revoke-runner`, run with `if: always()` so a failed deploy never leaves
port 22 open). The runner's IP alone isn't allowed as a bare `/32` - GitHub-hosted runners
don't reliably egress from one stable address for the whole job, so `cd.yml` looks up which of
GitHub's published Actions CIDR blocks (`api.github.com/meta`) contains the detected IP and
allows that whole block instead, covering the runner's actual address pool with one firewall
rule entry.

**No `ssh-keyscan`, host-key checking disabled for this connection.** The droplet's UFW ships
`22/tcp LIMIT` by default (DigitalOcean's `docker-20-04` image, not something this repo
configures) - it silently drops a source after 6 new connections within 30s. `ssh-keyscan`
makes several rapid probe connections to populate `known_hosts`, which is enough on its own to
trip that limit and make the *real* deploy connection a few seconds later fail with a
misleading `Connection timed out` (this cost real debugging time - see PRs #148-150). Instead,
the `Deploy` step sets `ANSIBLE_HOST_KEY_CHECKING=false` and skips host-key TOFU entirely for
this one ephemeral connection - the SSH private key is already the real trust boundary here.
Local operator deploys via `just ansible deploy` are unaffected; `ansible.cfg`'s default
host-key-checking behavior only changes for the CI job.

**Image pinning.** The `deploy` job passes `foray_app_image` as the exact digest the `publish`
job just built (`ghcr.io/jahrik/foray-planner@sha256:...`), not `:latest` - this closes the
race where a second push to `main` lands between build and deploy and the deploy step ends up
pulling a different image than the one that was just approved.

### One-time setup (operator, not part of any PR)

These are GitHub UI / DigitalOcean steps that can't be made by a code change:

1. **Generate a dedicated deploy keypair** - do not reuse your personal SSH key:
   ```bash
   ssh-keygen -t ed25519 -C foray-ci-deploy -f foray_ci_deploy_key -N ""
   ssh-copy-id -i foray_ci_deploy_key.pub root@$FORAY_DROPLET_IP   # run from an already-allowlisted machine
   ```
   Registering the key with DO by name isn't necessary - that's only consulted when a droplet
   is first created (`FORAY_SSH_KEY_NAME`); appending the public half to the droplet's
   `/root/.ssh/authorized_keys` directly is what actually grants SSH access to a running host.

   **Gotcha:** `ssh-copy-id` (and any `ssh -i foray_ci_deploy_key ...` test) can silently
   succeed via a *different* key than the one you intended, if your local `~/.ssh/config` has
   a `Host`/`IdentityFile` entry matching the droplet's hostname/IP from an earlier manual
   setup - SSH merges config-file identities with `-i`/agent-offered ones rather than
   restricting to just what you passed. `ssh-copy-id` then reports "all keys were skipped,
   already exist" without ever actually installing the new key, so a test that "works" isn't
   proof the CI key specifically is authorized. Verify with a clean config instead:
   ```bash
   ssh -F /dev/null -i foray_ci_deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
     root@$FORAY_DROPLET_IP 'echo OK'
   ```
   If that fails, append the public key directly rather than relying on `ssh-copy-id`:
   ```bash
   ssh root@$FORAY_DROPLET_IP "cat >> /root/.ssh/authorized_keys" < foray_ci_deploy_key.pub
   ```
2. In the repo's **Settings → Environments**, create an environment named `production` and add
   at least one required reviewer. Optionally restrict deployment branches to `main`.
3. Add these **environment-scoped** secrets on `production` (not repo-level secrets, so
   PR-triggered workflows from forks can't read them):

   | Secret | Value |
   |---|---|
   | `FORAY_DEPLOY_SSH_KEY` | Private half of the keypair from step 1 |
   | `DO_API_TOKEN` | Same DigitalOcean API token used for `just ansible provision` |
   | `FORAY_DROPLET_IP` | The droplet's public IP |
   | `FORAY_TLS_CERT` / `FORAY_TLS_KEY` | Contents (not paths) of the Cloudflare Origin CA cert/key - the CI equivalent of the local `FORAY_TLS_CERT_PATH`/`FORAY_TLS_KEY_PATH` files |
   | `RIDB_API_KEY` | Optional, same as the local `.env` value |

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
