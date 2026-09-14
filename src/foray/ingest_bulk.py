"""Bulk-snapshot ingest pipeline skeleton (issue #334 PR 1).

The machinery #335's per-source loaders (PAD-US, USFS trails, MTBS/RAVG) and #334 PR 2's
iNat/RIDB loaders build on: a source stages a dated snapshot to its own run-unique key space in
the DO Space (`foray.spaces.snapshot_run_prefix`) from GitHub Actions (`foray stage-snapshot`,
no droplet disk/bandwidth spent), then publishes a small manifest naming that run
(`foray.spaces.publish_snapshot`) once every object has landed. The droplet then loads whichever
snapshot is newest-published into Postgres (`foray ingest-bulk`) via a COPY-into-staging-then-swap
pattern (`copy_and_swap`) so a table is never read mid-load. Per-run key isolation + a single
atomic manifest write means a re-staged date, or two overlapping stage runs, can never produce a
mixed/partial read - see `foray.spaces` for the mechanics.

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
from foray.sources import camps, inat_bulk, ravg, usfs_mvum, usfs_trails
from foray.spaces import (
    list_snapshot_dates,
    new_run_id,
    prune_other_snapshots,
    publish_snapshot,
    snapshot_run_id,
)

logger = logging.getLogger(__name__)

# A stager fetches/transforms a source's data (GDAL/ogr2ogr, DuckDB, or a plain HTTP download -
# whatever the source needs) and uploads it under
# `spaces.snapshot_run_prefix(source, snapshot_date, run_id)` - its own private key space for
# this run, so re-staging a date or two overlapping runs can never collide. Runs from GitHub
# Actions, not the droplet - see `.github/workflows/bulk-load.yml`.
Stager = Callable[[Settings, date, str], None]

# A loader reads one published run (`spaces.snapshot_run_prefix(source, snapshot_date, run_id)`)
# and loads it into Postgres via `copy_and_swap`. Runs on the droplet (`foray ingest-bulk`).
Loader = Callable[[psycopg.Connection, Settings, date, str], None]

STAGERS: dict[str, Stager] = {
    "ridb": camps.stage_ridb,
    "inat": inat_bulk.stage_inat,
    "usfs_trails": usfs_trails.stage_usfs_trails,
    "usfs_mvum": usfs_mvum.stage_usfs_mvum,
    "ravg": ravg.stage_ravg,
}
LOADERS: dict[str, Loader] = {
    "ridb": camps.load_ridb,
    "inat": inat_bulk.load_inat,
    "usfs_trails": usfs_trails.load_usfs_trails,
    "usfs_mvum": usfs_mvum.load_usfs_mvum,
    "ravg": ravg.load_ravg,
}


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
    Space config - the CLI command turns those into a clean error message.

    Not safe to call twice concurrently for the same `source` (Copilot review, PR #361): the
    unconditional `prune_other_snapshots` call below deletes every object outside *this* call's
    run, which would delete a second concurrent call's not-yet-published objects out from under
    it. `.github/workflows/bulk-load.yml`'s `concurrency` group is what actually prevents that -
    this function has no in-process lock of its own, since GitHub Actions is the only place it
    ever runs from."""
    if not cfg.spaces.configured:
        raise RuntimeError("FORAY_SPACES__* not configured - see foray.config.Spaces")
    stager = STAGERS.get(source)
    if stager is None:
        raise KeyError(f"no stager registered for bulk source {source!r} (registered: {sorted(STAGERS)})")
    snapshot_date = snapshot_date or date.today()
    # A fresh run_id every call, even re-staging the same date - the stager uploads under its
    # own private prefix (spaces.snapshot_run_prefix) so a retry or an overlapping run can never
    # collide with another run's objects. publish_snapshot only flips the pointer once this
    # run's upload is fully done, so a reader can never observe a mix of two runs.
    run_id = new_run_id()
    stager(cfg, snapshot_date, run_id)
    publish_snapshot(cfg.spaces, source, snapshot_date, run_id)
    # Only after publish - see prune_other_snapshots' docstring for why pruning before the new
    # run is live would risk a concurrent loader losing the snapshot it's mid-download of.
    prune_other_snapshots(cfg.spaces, source, snapshot_date, run_id)
    return snapshot_date


def ingest_bulk(con: psycopg.Connection, cfg: Settings, source: str) -> date | None:
    """Load the newest published snapshot for `source` if it's newer than what's already
    loaded. Returns the snapshot date loaded, or ``None`` if there's nothing new."""
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
    last = last_loaded_snapshot(con, source)
    # <=, not != - the Space listing (or a database restored from a different point) can lack
    # the exact date last recorded, and an older available date must never load over a newer
    # already-loaded one.
    if last is not None and newest <= last:
        logger.info("ingest-bulk: %s already at snapshot %s (newest available: %s)", source, last, newest)
        return None
    run_id = snapshot_run_id(cfg.spaces, source, newest)
    assert run_id is not None, f"list_snapshot_dates returned {newest} but it has no published run"
    loader(con, cfg, newest, run_id)
    record_snapshot_loaded(con, source, newest)
    return newest
