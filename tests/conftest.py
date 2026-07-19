"""Shared Postgres test fixtures - the only "network" involved is the local/CI Postgres
service container (see docker-compose.yml / .github/workflows/ci.yml), same boundary the repo
already accepted for "hermetic" before this migration.

Isolation: one session-scoped connection + schema (created once via `cache.connect`'s DDL
bootstrap), truncate-all before every test. Not per-test transaction rollback: `api.py` uses a
connection *pool* post-migration, so a test seeding data on one connection and a request
reading via a different pooled connection would see nothing under naive rollback isolation.
Not fresh-schema-per-test either: recreating the `postgis` extension per test adds real
overhead across this many test files. Truncate is the standard, fast, pool-safe answer.
"""

from __future__ import annotations

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


@pytest.fixture(scope="session")
def _pg_session() -> Iterator[psycopg.Connection]:
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
