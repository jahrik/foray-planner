#!/usr/bin/env python3
"""One-off: populate `<table>.geom` for rows that predate the PostGIS Phase 0 migration (#268).

The BEFORE INSERT/UPDATE triggers keep `geom` current for every row written after the
migration lands; this fills in the rows already in the table. Idempotent and resumable - only
rows where `geom IS NULL` are touched - so a re-run picks up whatever a previous run left.

Connects via `foray.cache.connect()` (the standard PG* env vars): point it at prod by
exporting PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE, or leave unset for local dev.

Writes are batched with a short `lock_timeout` and `synchronous_commit = off`, and `--sleep`
paces the batches - a single set-based `UPDATE` on `observations` (~1.9M rows) or `trails`
(~1M) saturates the 1-vCPU managed box and starves the live server (same lesson as the DEM
elevation backfill, #238). Run the big tables behind the maintenance-page flag or spread over
off-peak hours with `--sleep`.

Usage: `just ansible backfill-geom-once` (prod), or
`uv run python scripts/backfill_geom.py [--dry-run] [--table observations] [--sleep 0.2]`.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import LiteralString

import psycopg

from foray.cache import connect

# table -> (SQL expression for geom, extra WHERE predicate). Point tables derive from lat/lng
# (never throws); layer tables parse the GeoJSON text and can throw on a malformed feature, so
# those batches fall back to row-by-row on error (see _fill). All LiteralString - composed into
# statement text, never request data.
_POINT_EXPR: LiteralString = "ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography"
_GEOJSON_EXPR: LiteralString = "ST_MakeValid(ST_GeomFromGeoJSON(geojson))::geography"

TABLES: dict[LiteralString, tuple[LiteralString, LiteralString]] = {
    "campsites": (_POINT_EXPR, "lat IS NOT NULL AND lng IS NOT NULL"),
    "observations": (_POINT_EXPR, "lat IS NOT NULL AND lng IS NOT NULL"),
    "trails": (_GEOJSON_EXPR, "geojson IS NOT NULL"),
    "public_land": (_GEOJSON_EXPR, "geojson IS NOT NULL"),
    "fire_perimeters": (_GEOJSON_EXPR, "geojson IS NOT NULL"),
}


def _pending(con: psycopg.Connection, table: LiteralString, predicate: LiteralString) -> int:
    row = con.execute(f"SELECT count(*) FROM {table} WHERE geom IS NULL AND {predicate}").fetchone()
    return int(row[0]) if row else 0


def _fill(
    con: psycopg.Connection,
    table: LiteralString,
    expr: LiteralString,
    predicate: LiteralString,
    *,
    batch_size: int,
    sleep_s: float,
    dry_run: bool,
) -> tuple[int, int]:
    """Fill `geom` for one table, batch by batch. Returns `(filled, skipped_bad_geometry)`."""
    if dry_run:
        return _pending(con, table, predicate), 0
    filled = skipped = 0
    bad_ids: list[object] = []  # rows a row-by-row retry proved unparseable - don't re-select them
    # id is the primary key on every table here (bigint for observations, text elsewhere).
    select_batch = f"SELECT id FROM {table} WHERE geom IS NULL AND {predicate} AND id <> ALL(%s) LIMIT %s"
    while True:
        ids = [row[0] for row in con.execute(select_batch, [bad_ids, batch_size]).fetchall()]
        if not ids:
            break
        update = f"UPDATE {table} SET geom = {expr} WHERE id = ANY(%s) AND geom IS NULL"
        try:
            with con.transaction(), con.cursor() as cur:
                cur.execute("SET LOCAL synchronous_commit = off")
                cur.execute(update, [ids])
                filled += cur.rowcount or 0
        except psycopg.OperationalError as exc:  # lock_timeout / transient - leave for a re-run
            print(f"  ! {table}: batch stalled on a lock, left for re-run: {exc}")
            break
        except psycopg.Error:
            # A malformed GeoJSON feature somewhere in the batch aborted it - retry row by row
            # so the one bad row is skipped (left with geom NULL) and the rest still land.
            for one in ids:
                try:
                    with con.transaction(), con.cursor() as cur:
                        cur.execute("SET LOCAL synchronous_commit = off")
                        cur.execute(update, [[one]])
                        filled += cur.rowcount or 0
                except psycopg.OperationalError as exc:
                    print(f"  ! {table}: batch stalled on a lock, left for re-run: {exc}")
                    return filled, skipped
                except psycopg.Error:
                    skipped += 1
                    bad_ids.append(one)
                    print(f"  ! {table} id={one!r}: bad geometry, left NULL")
        if sleep_s:
            time.sleep(sleep_s)
    return filled, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report row counts, write nothing")
    parser.add_argument("--table", choices=sorted(TABLES), action="append", help="limit to this table (repeatable)")
    parser.add_argument("--batch-size", type=int, default=5000, help="rows per UPDATE batch (default 5000)")
    parser.add_argument("--sleep", type=float, default=0.0, metavar="SECONDS", help="pause between batches (default 0)")
    args = parser.parse_args()

    con = connect()
    if not args.dry_run:
        con.execute("SET lock_timeout = '5s'")
        con.execute("SET statement_timeout = '120s'")

    tables: list[LiteralString] = [t for t in TABLES if not args.table or t in args.table]
    total_skipped = 0
    for table in tables:
        expr, predicate = TABLES[table]
        pending = _pending(con, table, predicate)
        print(f"{table}: {pending:,} rows need geom")
        if not pending:
            continue
        started = time.monotonic()
        filled, skipped = _fill(
            con, table, expr, predicate, batch_size=args.batch_size, sleep_s=args.sleep, dry_run=args.dry_run
        )
        total_skipped += skipped
        remaining = _pending(con, table, predicate)
        print(
            f"{table}: {'would fill' if args.dry_run else 'filled'} {filled:,}"
            f"{f', skipped {skipped:,} bad' if skipped else ''}"
            f" in {time.monotonic() - started:.0f}s; {remaining:,} still NULL"
        )

    # Non-zero exit if any table still has pending rows (a stalled batch) so the Ansible task
    # and an operator see the run as incomplete - a re-run only retries what's left.
    incomplete = any(_pending(con, t, TABLES[t][1]) for t in tables)
    return 1 if incomplete and not args.dry_run else (1 if total_skipped else 0)


if __name__ == "__main__":
    sys.exit(main())
