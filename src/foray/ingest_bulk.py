"""Bulk-snapshot ingest pipeline skeleton (issue #334 PR 1).

The machinery #335's per-source loaders (PAD-US, USFS trails, MTBS/RAVG) and #334 PR 2's
iNat/RIDB loaders build on: a source stages a dated snapshot to the DO Space
(``bulk/{source}/{date}/...``, see `foray.spaces.snapshot_prefix`) from GitHub Actions
(`foray stage-snapshot`, no droplet disk/bandwidth spent), then the droplet loads whichever
snapshot is newest into Postgres (`foray ingest-bulk`) via a COPY-into-staging-then-swap
pattern (`copy_and_swap`) so a table is never read mid-load.

No source is registered yet - `STAGERS`/`LOADERS` are empty, and both CLI commands raise a
clear error for any source name until #334 PR 2 / #335 add entries. The registry + generic
helpers are what this PR actually ships.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import LiteralString

import psycopg

from foray.config import Settings
from foray.spaces import list_snapshot_dates

logger = logging.getLogger(__name__)

# A stager fetches/transforms a source's data (GDAL/ogr2ogr, DuckDB, or a plain HTTP download -
# whatever the source needs) and uploads it under `spaces.snapshot_prefix(source, today)`.
# Runs from GitHub Actions, not the droplet - see `.github/workflows/bulk-load.yml`.
Stager = Callable[[Settings, date], None]

# A loader reads one already-staged snapshot (`spaces.snapshot_prefix(source, snapshot_date)`)
# and loads it into Postgres via `copy_and_swap`. Runs on the droplet (`foray ingest-bulk`).
Loader = Callable[[psycopg.Connection, Settings, date], None]

STAGERS: dict[str, Stager] = {}
LOADERS: dict[str, Loader] = {}


def copy_and_swap(
    con: psycopg.Connection,
    table: LiteralString,
    create_staging_sql: LiteralString,
    copy_fn: Callable[[psycopg.Connection, str], None],
    swap_sql: LiteralString,
) -> None:
    """Load a bulk table without ever exposing a partially-loaded one to a live query.

    `create_staging_sql` creates ``{table}_staging`` (empty). `copy_fn` is called with the
    connection and the staging table name to actually load rows into it (a `COPY FROM` against
    a snapshot file, or several - source-specific, not this function's concern). `swap_sql` then
    atomically replaces `table` with the staging table's contents (typically a
    transaction-wrapped ``DROP``/rename pair, or a partitioned table's ``ATTACH``/``DETACH``).
    The whole thing runs in one transaction: a failure at any step leaves `table` untouched.
    """
    staging: LiteralString = f"{table}_staging"
    with con.transaction():
        con.execute(f"DROP TABLE IF EXISTS {staging}")
        con.execute(create_staging_sql)
        copy_fn(con, staging)
        con.execute(swap_sql)


def _meta_key(source: str) -> str:
    return f"bulk_snapshot:{source}"


def last_loaded_snapshot(con: psycopg.Connection, source: str) -> date | None:
    row = con.execute("SELECT value FROM meta WHERE key = %s", [_meta_key(source)]).fetchone()
    return None if row is None else date.fromisoformat(row[0])


def record_snapshot_loaded(con: psycopg.Connection, source: str, snapshot_date: date) -> None:
    con.execute(
        "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        [_meta_key(source), snapshot_date.isoformat()],
    )


def stage_snapshot(cfg: Settings, source: str, snapshot_date: date | None = None) -> date:
    """Fetch + upload today's (or `snapshot_date`'s) snapshot for `source`. Raises
    `click.ClickException`-friendly `KeyError`/`RuntimeError` for an unknown source or missing
    Space config - the CLI command turns those into a clean error message."""
    if not cfg.spaces.configured:
        raise RuntimeError("FORAY_SPACES__* not configured - see foray.config.Spaces")
    stager = STAGERS.get(source)
    if stager is None:
        raise KeyError(f"no stager registered for bulk source {source!r} (registered: {sorted(STAGERS)})")
    snapshot_date = snapshot_date or date.today()
    stager(cfg, snapshot_date)
    return snapshot_date


def ingest_bulk(con: psycopg.Connection, cfg: Settings, source: str) -> date | None:
    """Load the newest staged snapshot for `source` if it's newer than what's already loaded.
    Returns the snapshot date loaded, or ``None`` if nothing new was staged."""
    if not cfg.spaces.configured:
        raise RuntimeError("FORAY_SPACES__* not configured - see foray.config.Spaces")
    loader = LOADERS.get(source)
    if loader is None:
        raise KeyError(f"no loader registered for bulk source {source!r} (registered: {sorted(LOADERS)})")
    available = list_snapshot_dates(cfg.spaces, source)
    if not available:
        logger.info("ingest-bulk: no snapshots staged yet for %s", source)
        return None
    newest = available[-1]
    if newest == last_loaded_snapshot(con, source):
        logger.info("ingest-bulk: %s already at snapshot %s", source, newest)
        return None
    loader(con, cfg, newest)
    record_snapshot_loaded(con, source, newest)
    return newest
