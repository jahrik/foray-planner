# Common dev/app operations. Ansible (DigitalOcean deploy) recipes live in the `ansible`
# module - run `just ansible`. Requires `just` (uv tool install rust-just).

mod ansible 'just/ansible.just'

# Put an nvm-installed Node matching frontend/.nvmrc on PATH if one exists; otherwise assume
# `node` is already resolvable (e.g. a system install). Keeps the pinned major in one place.
node_major := `cat frontend/.nvmrc 2>/dev/null || true`
node_bin := if node_major == "" { "" } else { `ls -d "$HOME"/.nvm/versions/node/v"$(cat frontend/.nvmrc)"*/bin 2>/dev/null | head -n1` }

export PATH := if node_bin == "" { env_var("PATH") } else { node_bin + ":" + env_var("PATH") }
export PGHOST := env_var_or_default("PGHOST", "localhost")
export PGPORT := env_var_or_default("PGPORT", "5432")
export PGUSER := env_var_or_default("PGUSER", "foray")
export PGPASSWORD := env_var_or_default("PGPASSWORD", "foray")
export PGDATABASE := env_var_or_default("PGDATABASE", "foray")

# List available recipes.
default:
    @just --list

# --- dev ---

# uv sync + frontend npm ci.
[group('dev')]
install:
    uv sync
    cd frontend && npm ci

# Check-only (not auto-fixing) so this is a true verification step, safe for CI - the
# ruff-format/ruff-check pre-commit hooks own auto-fixing on commit.
[doc('ruff format --check + ruff check + ty + vulture')]
[group('dev')]
lint:
    uv run ruff format --check .
    uv run ruff check .
    uv run ty check
    uv run vulture src/foray --min-confidence 80

# Start Postgres if needed, then run pytest.
[group('dev')]
test: db
    uv run pytest

# The full local CI gate: lint + test.
[group('dev')]
check: lint test

# Assumes `frontend/node_modules` already exists (`just install` or CI's `npm ci`) - this
# only runs lint + type-check + build, not the install.
[doc('Frontend lint + test + type-check + build')]
[group('dev')]
frontend:
    cd frontend && npm run lint && npm test && npm run build

# Regenerates the OpenAPI-derived frontend types (needs `uv` for `foray openapi` + `npm` for
# `openapi-typescript`, so `frontend/node_modules` and the Python venv must already exist) and
# fails if that produces a diff - catches a backend response shape drifting from schema.ts
# without anyone remembering to run `npm run gen:api` (see issue #98).
[doc('Regenerate frontend API types and fail on drift')]
[group('dev')]
check-api-schema:
    cd frontend && npm run gen:api
    git diff --exit-code frontend/src/api/schema.ts

# One-off diagnostic query against local dev data, e.g. `just psql "SELECT count(*) FROM observations"`.
# No local psql client needed - runs inside the postgres container.
[doc('Run a one-off SQL query inside the postgres container')]
[group('dev')]
psql sql: db
    docker compose exec -T postgres psql -U foray -d foray -c "{{ sql }}"

# --- services ---

# Start Postgres (docker compose) and wait for it to accept connections.
[group('services')]
db:
    docker compose up -d postgres
    @echo "Waiting for Postgres…"
    @until docker compose exec -T postgres pg_isready -U foray -q 2>/dev/null; do sleep 0.5; done
    @echo "Postgres ready."

# Build + start app + postgres (http://localhost:8000).
[group('services')]
start:
    docker compose up -d --build

# Full teardown + rebuild - use this (not `start`) when a code change needs to land in a
# container that's already running. `--force-recreate` looks like the obvious tool for that,
# but it fights podman-compose's shared-pod model (it tries to recreate one container while
# its pod-mates are still up, which podman-compose can't sequence, and repeatedly corrupted
# the pod's DNS in testing) - a full `down` first sidesteps that entirely. Both commands include
# `--profile scheduler` so the scheduler container (if it was up) comes back up too, rather than
# staying torn down after a restart.
[doc('Full teardown + rebuild (use when a code change must land in a running container)')]
[group('services')]
restart:
    docker compose --profile scheduler down
    docker compose --profile scheduler up -d --build

# Start the background scheduler container (observation + layer refresh loops).
[group('services')]
scheduler:
    docker compose --profile scheduler up -d --build scheduler

# Stop all containers (including scheduler if running).
[group('services')]
stop:
    docker compose --profile scheduler stop

# Tear down containers + volumes.
[group('services')]
clean:
    docker compose --profile scheduler down -v

# --- data ---

# One-shot all-regions ingest + phenology rebuild.
[group('data')]
ingest: db
    docker compose run --rm app foray ingest --countries

# Refresh the fungi_genera catalog from iNat.
[group('data')]
genera-refresh: db
    docker compose run --rm app foray genera-refresh

# Re-checks cached observations under genera whose cache count has drifted from iNat's live
# count (see ingest.revalidate) - purges/reassigns rows misidentified into a homonymous
# non-fungal genus (e.g. fungal Olla vs. the ladybug genus Olla). Meant to run on a recurring
# schedule (scripts/scheduler.sh); this recipe is for running it on demand against local dev data.
[doc('On-demand genus-drift revalidation against local dev data')]
[group('data')]
revalidate: db
    docker compose run --rm app foray revalidate

# Re-checks the *whole* observations cache against iNat, oldest/never-checked first (see
# ingest.resync) - the only path that eventually trues up every column (including `obscured`,
# never set by the bulk historical import) and catches a misidentification too rare within its
# genus for `revalidate`'s ratio check to flag. Default: one on-demand batch, same shape
# scripts/scheduler.sh runs hourly. Pass args for a deliberate catch-up run instead - e.g.
# `just resync "--until-done --batch-size 20000"` keeps going batch after batch until every
# row has been live-checked at least once (long-running, rate-limited by iNat ~1 req/s; run in
# the background) - use after finding a data-accuracy bug, not as a routine invocation.
[doc('On-demand observations-cache resync batch (pass args for a catch-up run)')]
[group('data')]
resync *args: db
    docker compose run --rm app foray resync {{ args }}

# One-time (or rebuild-from-scratch) bulk-load path for issue #79 Phase 3 - the nightly ingest
# cron keeps things fresh day-to-day, so these are opt-in, not part of `check`/`start`. ~25.5GB
# download, run on the host (not in a container) since it just needs `curl` and a place to land
# data/ - `-C -` resumes an interrupted download instead of restarting it.
[doc('Download the ~25.5GB iNat GBIF DwC-A archive (resumable)')]
[group('data')]
bulk-download:
    mkdir -p data
    curl -L -C - --fail -o data/gbif-observations-dwca.zip \
        https://static.inaturalist.org/observations/gbif-observations-dwca.zip

# Multi-hour full scan of the ~208M-row archive - needs the fungi_genera catalog populated
# first (`just genera-refresh`).
[doc('Full scan of the DwC-A archive for fungi observations (multi-hour)')]
[group('data')]
bulk-filter:
    uv run python scripts/inat_dwca_filter.py

# Load the filtered bulk observations into Postgres.
[group('data')]
bulk-load: db
    uv run python scripts/load_inat_bulk.py

# --- deps ---

# Regenerates all three lockfiles (root uv.lock, frontend package-lock.json, ansible uv.lock)
# against their current pyproject.toml/package.json constraints.
[doc('Re-resolve all three lockfiles against current constraints')]
[group('deps')]
lock:
    uv lock
    cd frontend && npm install
    cd infra/ansible && uv lock

# Upgrades all three lockfiles to the newest versions allowed by their current
# pyproject.toml/package.json constraints, rather than just re-resolving against what's
# already locked (see `lock`).
[doc('Upgrade all three lockfiles to newest allowed versions')]
[group('deps')]
patch:
    uv lock --upgrade
    cd frontend && npm update
    cd infra/ansible && uv lock --upgrade
