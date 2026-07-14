.PHONY: db install lint test check frontend start stop scheduler clean ingest

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
	docker compose up -d --build --force-recreate

scheduler:
	docker compose --profile scheduler up -d --build scheduler

stop:
	docker compose --profile scheduler stop

ingest: db
	docker compose run --rm app foray ingest --all-regions

clean:
	docker compose --profile scheduler down -v
