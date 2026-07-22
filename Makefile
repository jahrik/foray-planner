.PHONY: db psql install lint test check frontend check-api-schema start restart stop scheduler clean \
	ingest genera-refresh revalidate resync backfill-obscured bulk-download bulk-filter bulk-load \
	ansible-install ansible-lint ansible-deploy ansible-provision ansible-ingest-once \
	ansible-genera-once ansible-bulk-load-once

NODE_BIN := $(HOME)/.nvm/versions/node/v24.18.0/bin
export PATH := $(NODE_BIN):$(PATH)
export PGHOST ?= localhost
export PGPORT ?= 5432
export PGUSER ?= foray
export PGPASSWORD ?= foray
export PGDATABASE ?= foray

db:
	docker compose up -d postgres
	@echo "Waiting for Postgres…"
	@until docker compose exec -T postgres pg_isready -U foray -q 2>/dev/null; do sleep 0.5; done
	@echo "Postgres ready."

# One-off diagnostic queries against local dev data, e.g. `make psql SQL="SELECT count(*) FROM observations"`.
# No local psql client needed - runs inside the postgres container.
psql: db
	docker compose exec -T postgres psql -U foray -d foray -c "$(SQL)"

install:
	uv sync
	cd frontend && npm ci

# Check-only (not auto-fixing) so this is a true verification step, safe for CI - the
# ruff-format/ruff-check pre-commit hooks own auto-fixing on commit.
lint:
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check

test: db
	uv run pytest

check: lint test

# Assumes `frontend/node_modules` already exists (`make install` or CI's `npm ci`) - this
# only runs the type-check + build, not the install.
frontend:
	cd frontend && npm run build

# Regenerates the OpenAPI-derived frontend types (needs `uv` for `foray openapi` + `npm` for
# `openapi-typescript`, so `frontend/node_modules` and the Python venv must already exist) and
# fails if that produces a diff - catches a backend response shape drifting from schema.ts
# without anyone remembering to run `npm run gen:api` (see issue #98).
check-api-schema:
	cd frontend && npm run gen:api
	git diff --exit-code frontend/src/api/schema.ts

start:
	docker compose up -d --build

# Full teardown + rebuild - use this (not `start`) when a code change needs to land in a
# container that's already running. `--force-recreate` looks like the obvious tool for that,
# but it fights podman-compose's shared-pod model (it tries to recreate one container while
# its pod-mates are still up, which podman-compose can't sequence, and repeatedly corrupted
# the pod's DNS in testing) - a full `down` first sidesteps that entirely. Both commands include
# `--profile scheduler` so the scheduler container (if it was up) comes back up too, rather than
# staying torn down after a restart.
restart:
	docker compose --profile scheduler down
	docker compose --profile scheduler up -d --build

scheduler:
	docker compose --profile scheduler up -d --build scheduler

stop:
	docker compose --profile scheduler stop

ingest: db
	docker compose run --rm app foray ingest --countries

genera-refresh: db
	docker compose run --rm app foray genera-refresh

# Re-checks cached observations under genera whose cache count has drifted from iNat's live
# count (see ingest.revalidate) - purges/reassigns rows misidentified into a homonymous
# non-fungal genus (e.g. fungal Olla vs. the ladybug genus Olla). Meant to run on a recurring
# schedule (scripts/scheduler.sh), this target is for running it on demand against local dev data.
revalidate: db
	docker compose run --rm app foray revalidate

# Re-checks the *whole* observations cache against iNat, oldest/never-checked first (see
# ingest.resync) - the only path that eventually trues up every column (including
# `obscured`, never set by the bulk historical import) and catches a misidentification too rare
# within its genus for `revalidate`'s ratio check to flag. Default: one on-demand batch, same
# shape scripts/scheduler.sh runs hourly. Pass ARGS for a deliberate catch-up run instead - e.g.
# `make resync ARGS="--until-done --batch-size 20000"` keeps going batch after batch until every
# row has been live-checked at least once (long-running, rate-limited by iNat ~1 req/s; run in
# the background) - use after finding a data-accuracy bug, not as a routine invocation.
resync: db
	docker compose run --rm app foray resync $(ARGS)

# One-time heuristic fix for the bulk-historical-import rows whose `obscured` flag was never set
# (see scripts/backfill_obscured.py) - a NULL flag makes the UI show iNat's randomized decoy
# coordinate for a geoprivacy-obscured observation as if it were the real, precise location.
# Safe to re-run (only touches still-NULL rows); `make resync`'s ongoing grind corrects
# the ~1.7% heuristic false positives with the real flag over time. Not part of the foray CLI -
# same one-time-script pattern as bulk-load (see that target's comment).
backfill-obscured: db
	uv run python scripts/backfill_obscured.py

# One-time (or rebuild-from-scratch) bulk-load path for issue #79 Phase 3 - the nightly
# ingest cron keeps things fresh day-to-day, so these are opt-in, not part of `check`/`start`.
# ~25.5GB download, run on the host (not in a container) since it just needs `curl` and a
# place to land data/ - `-C -` resumes an interrupted download instead of restarting it.
bulk-download:
	mkdir -p data
	curl -L -C - --fail -o data/gbif-observations-dwca.zip \
		https://static.inaturalist.org/observations/gbif-observations-dwca.zip

# Multi-hour full scan of the ~208M-row archive - needs the fungi_genera catalog populated
# first (`make genera-refresh`).
bulk-filter:
	uv run python scripts/inat_dwca_filter.py

bulk-load: db
	uv run python scripts/load_inat_bulk.py

clean:
	docker compose --profile scheduler down -v

# --- Ansible (Digital Ocean deployment) ---
# Required env vars for provisioning: DO_API_TOKEN, FORAY_SSH_KEY_NAME, FORAY_SSH_ALLOWED_IPS
# Required env vars for deploy-only: FORAY_DROPLET_IP (+ DB creds in /opt/foray-planner/foray.env)

ANSIBLE_DIR := infra/ansible

ansible-install:
	cd $(ANSIBLE_DIR) && uv sync
	cd $(ANSIBLE_DIR) && uv run ansible-galaxy collection install -r requirements.yml

ansible-lint:
	cd $(ANSIBLE_DIR) && uv run yamllint .
	cd $(ANSIBLE_DIR) && uv run ansible-lint

ansible-provision:
	@test -n "$$DO_API_TOKEN" || (echo "ERROR: DO_API_TOKEN not set" && exit 1)
	@test -n "$$FORAY_SSH_KEY_NAME" || (echo "ERROR: FORAY_SSH_KEY_NAME not set" && exit 1)
	@test -n "$$FORAY_SSH_ALLOWED_IPS" || (echo "ERROR: FORAY_SSH_ALLOWED_IPS not set" && exit 1)
	cd $(ANSIBLE_DIR) && uv run ansible-playbook site.yml --tags foray:provision \
		-e foray_do_ssh_key_name=$$FORAY_SSH_KEY_NAME \
		-e '{"foray_ssh_allowed_ips": ["'"$$FORAY_SSH_ALLOWED_IPS"'"]}'

ansible-deploy:
	@test -n "$$FORAY_DROPLET_IP" || (echo "ERROR: FORAY_DROPLET_IP not set" && exit 1)
	cd $(ANSIBLE_DIR) && uv run ansible-playbook site.yml --tags foray:deploy \
		-i inventory/hosts.yml \
		-e foray_droplet_ip=$$FORAY_DROPLET_IP

# Manual/opt-in only - the foray-genera cron job already keeps the catalog fresh on a
# schedule. Use this to warm fungi_genera immediately on a fresh droplet (depends on the env
# file that deploy renders), instead of waiting for the next cron run. Run this *before*
# ansible-ingest-once - ingest fails fast if the catalog is empty.
ansible-genera-once:
	@test -n "$$FORAY_DROPLET_IP" || (echo "ERROR: FORAY_DROPLET_IP not set" && exit 1)
	cd $(ANSIBLE_DIR) && uv run ansible-playbook site.yml --tags foray:genera-once \
		-i inventory/hosts.yml \
		-e foray_droplet_ip=$$FORAY_DROPLET_IP

# Manual/opt-in only - the foray-ingest cron job already keeps data fresh on a schedule.
# Use this to warm data immediately after the *first* `ansible-deploy` on a fresh droplet
# (depends on the env file that deploy renders), instead of waiting for the next cron run.
# Not part of `ansible-deploy` itself.
ansible-ingest-once:
	@test -n "$$FORAY_DROPLET_IP" || (echo "ERROR: FORAY_DROPLET_IP not set" && exit 1)
	cd $(ANSIBLE_DIR) && uv run ansible-playbook site.yml --tags foray:ingest-once \
		-i inventory/hosts.yml \
		-e foray_droplet_ip=$$FORAY_DROPLET_IP

# One-off: push the already-filtered, already-verified data/inat_us_observations.jsonl
# (see `make bulk-filter`) to the droplet and load it - the fast path for issue #79 Phase 3b
# instead of waiting on the nightly ingest cron to backfill history via the iNat API. Run
# `make ansible-genera-once` first if the catalog hasn't been warmed yet.
ansible-bulk-load-once:
	@test -n "$$FORAY_DROPLET_IP" || (echo "ERROR: FORAY_DROPLET_IP not set" && exit 1)
	cd $(ANSIBLE_DIR) && uv run ansible-playbook site.yml --tags foray:bulk-load-once \
		-i inventory/hosts.yml \
		-e foray_droplet_ip=$$FORAY_DROPLET_IP
