"""Shared Postgres test fixtures - the only "network" involved is the local/CI Postgres
service container (see docker-compose.yml / .github/workflows/ci.yml), same boundary the repo
already accepted for "hermetic" before this migration.

Isolation: one session-scoped connection + schema (created once via `cache.connect`'s DDL
bootstrap), truncate-all before every test. Not per-test transaction rollback: `api.py` uses a
connection *pool* post-migration, so a test seeding data on one connection and a request
reading via a different pooled connection would see nothing under naive rollback isolation.
Not fresh-schema-per-test either: recreating the `postgis` extension per test adds real
overhead across this many test files. Truncate is the standard, fast, pool-safe answer.

Runs against its own `foray_test` database (created here on first run if missing), not the
`foray` database local dev/QA uses - so `make test` truncating tables can't wipe real data
you loaded into local dev Postgres (e.g. via `make genera-refresh`). Same Postgres server/
container either way, just a different database on it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

from foray.cache import connect

_TABLES = (
    "observations",
    "taxa",
    "campsites",
    "public_land",
    "trails",
    "ingest_log",
    "app_location",
    "fungi_genera",
    "app_genera",
)

_TEST_DB_NAME = "foray_test"


def _ensure_test_database() -> None:
    with psycopg.connect(dbname="postgres", autocommit=True) as admin:
        exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", [_TEST_DB_NAME]).fetchone()
        if not exists:
            admin.execute(f"CREATE DATABASE {_TEST_DB_NAME}")


@pytest.fixture(scope="session")
def _pg_session() -> Iterator[psycopg.Connection]:
    _ensure_test_database()
    os.environ["PGDATABASE"] = _TEST_DB_NAME
    conn = connect()
    yield conn
    conn.close()


@pytest.fixture
def con(_pg_session: psycopg.Connection) -> psycopg.Connection:
    """A truncated-clean connection to the shared test Postgres, schema already applied."""
    _pg_session.execute("DROP TABLE IF EXISTS phenology")
    _pg_session.execute("DROP TABLE IF EXISTS regions")
    _pg_session.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
    return _pg_session
