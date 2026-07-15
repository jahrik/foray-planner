.PHONY: db install lint test check frontend start restart stop scheduler clean ingest \
	ansible-install ansible-lint ansible-deploy ansible-provision ansible-ingest-once

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

install:
	uv sync
	cd frontend && npm ci

lint:
	uv run ruff format .
	uv run ruff check .
	uv run ty check

test: db
	uv run pytest

check: lint test

frontend:
	cd frontend && npm run build

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
	docker compose run --rm app foray ingest --all-regions

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

# Manual/opt-in only - the foray-ingest cron job already keeps data fresh on a schedule.
# Use this to warm data immediately (e.g. right after provisioning a fresh droplet) without
# waiting for the next cron run; not part of `ansible-deploy`.
ansible-ingest-once:
	@test -n "$$FORAY_DROPLET_IP" || (echo "ERROR: FORAY_DROPLET_IP not set" && exit 1)
	cd $(ANSIBLE_DIR) && uv run ansible-playbook site.yml --tags foray:ingest-once \
		-i inventory/hosts.yml \
		-e foray_droplet_ip=$$FORAY_DROPLET_IP
