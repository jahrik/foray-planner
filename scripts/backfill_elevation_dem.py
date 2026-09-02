#!/usr/bin/env python3
"""One-off: backfill `observations.elevation_m` from local Copernicus GLO-90 DEM tiles.

The hourly `foray-backfill-elevation` cron drains the backlog through Open-Meteo's free
elevation API, but that tier caps at ~10k points/day - against a multi-million-row backlog
that is ~200 days of trickle. This script samples the *same* DEM (Copernicus GLO-90, ~90 m,
nearest-cell - so values stay consistent with rows already enriched via `foray.sources.elevation`)
from 1x1 degree Cloud-Optimized GeoTIFF tiles instead, pulled once from the public AWS Open
Data mirror (`s3://copernicus-dem-90m`, no credentials) into a local cache. The whole backlog
then clears in one pass: minutes of CPU once the tiles are down.

Connects to Postgres via `foray.cache.connect()`, i.e. the standard PG* env vars - point it at
prod by exporting PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE (e.g.
`set -a; source foray.env; set +a`) or leave unset for local dev. Tiles cache under
`FORAY_DEM_CACHE` (default `~/.cache/foray/dem`); set it to a mounted volume when running in
the container so a re-run does not re-download.

Idempotent and resumable: only rows where `elevation_m IS NULL` are touched, and cached tiles
plus `.missing` markers (for the all-ocean cells the mirror does not publish) are reused.

Writes are set-based: each cell's sampled values go through a `COPY` into a TEMP table and a
single `UPDATE ... FROM`, chunked at `--batch-size`, with `synchronous_commit = off` and a
short `lock_timeout` on the session. A naive `executemany` of ~1.8M single-row UPDATEs runs
at ~180 rows/s against a network-attached managed Postgres and starves the live server of
locks and WAL bandwidth (it took prod's `/api/destinations` down once); this stays out of
its way. Use `--sleep` to pace batches further and `--max-cells` to run in slices.

Usage: `just ansible backfill-elevation-dem-once` (prod), or
`uv run --with rasterio python scripts/backfill_elevation_dem.py [--dry-run] [--no-rebuild]`.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import psycopg
import rasterio

from foray.cache import connect
from foray.config import Settings
from foray.scoring import build_phenology

TILE_BUCKET = "https://copernicus-dem-90m.s3.amazonaws.com"
TILE_PREFIX = "Copernicus_DSM_COG_30"
DOWNLOAD_TIMEOUT_S = 180.0

# Only research-grade, unobscured, in-range rows - the same filter scoring reads (obscured
# points carry iNat's randomized decoy coordinate, so their elevation would be meaningless).
# The lat/lng bounds are half-open to match the tile grid: a 1x1 degree tile is keyed on its
# south-west corner over [-90, 90) x [-180, 180), so lat=90 / lng=180 would name a tile
# (N90 / E180) the mirror does not publish. Any such extreme-boundary row is left for the
# Open-Meteo backfill path.
ELIGIBLE = (
    "elevation_m IS NULL AND quality_grade = 'research' AND NOT COALESCE(obscured, false) "
    "AND lat >= -90 AND lat < 90 AND lng >= -180 AND lng < 180"
)


def cache_dir() -> Path:
    path = Path(os.environ.get("FORAY_DEM_CACHE") or str(Path.home() / ".cache" / "foray" / "dem"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def tile_id(south: int, west: int) -> str:
    """GLO-90 tile id for the 1x1 degree cell with this south-west corner (latitude/longitude
    floored toward negative infinity)."""
    ns = f"N{south:02d}" if south >= 0 else f"S{-south:02d}"
    ew = f"E{west:03d}" if west >= 0 else f"W{-west:03d}"
    return f"{TILE_PREFIX}_{ns}_00_{ew}_00_DEM"


def fetch_tile(cache: Path, tid: str) -> tuple[str, str]:
    """Ensure ``tid`` is cached. Returns ``(tid, status)`` where status is ``cached`` /
    ``downloaded`` / ``ocean`` (mirror has no such tile - all-ocean cells are unpublished) /
    an error string (transient - a re-run retries it)."""
    tif = cache / f"{tid}.tif"
    if tif.exists():
        return tid, "cached"
    if (cache / f"{tid}.missing").exists():
        return tid, "ocean"
    tmp = tif.with_suffix(".part")
    try:
        with httpx.stream(
            "GET", f"{TILE_BUCKET}/{tid}/{tid}.tif", timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True
        ) as resp:
            if resp.status_code == 404:
                (cache / f"{tid}.missing").touch()
                return tid, "ocean"
            if resp.status_code != 200:
                return tid, f"http {resp.status_code}"
            with tmp.open("wb") as handle:
                for chunk in resp.iter_bytes():
                    handle.write(chunk)
    except httpx.HTTPError as error:
        tmp.unlink(missing_ok=True)
        return tid, f"error: {error}"
    tmp.replace(tif)
    return tid, "downloaded"


def _batched[T](seq: Sequence[T], size: int) -> list[Sequence[T]]:
    """Split ``seq`` into consecutive slices of at most ``size`` (``size <= 0`` -> one slice)."""
    if size <= 0:
        return [seq]
    return [seq[start : start + size] for start in range(0, len(seq), size)]


def apply_updates(
    con: psycopg.Connection, updates: Sequence[tuple[int, int]], *, batch_size: int, sleep_s: float
) -> tuple[int, int]:
    """Write ``(obs_id, elevation_m)`` pairs to ``observations`` set-based: per batch, ``COPY``
    into a session TEMP table then one ``UPDATE ... FROM`` joined on the primary key. Returns
    ``(applied, stalled)`` - a batch whose transaction times out on a lock (the live server
    holding it) is counted as stalled and left for a re-run rather than blocking on it."""
    # Created once (IF NOT EXISTS - this runs per cell); rows cleared at each COMMIT.
    con.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _elev_batch (id bigint PRIMARY KEY, elevation_m int) ON COMMIT DELETE ROWS"
    )
    applied = stalled = 0
    for batch in _batched(updates, batch_size):
        try:
            with con.transaction(), con.cursor() as cur:
                # Idempotent + resumable, so a crash that loses the last committed batch just
                # gets redone on re-run - not worth an fsync round-trip per batch to a network DB.
                cur.execute("SET LOCAL synchronous_commit = off")
                with cur.copy("COPY _elev_batch (id, elevation_m) FROM STDIN") as copy:
                    for obs_id, value in batch:
                        copy.write_row((obs_id, value))
                # AND elevation_m IS NULL: never clobber a value the hourly Open-Meteo cron
                # may have written into this cell since the rows were selected.
                cur.execute(
                    "UPDATE observations o SET elevation_m = t.elevation_m "
                    "FROM _elev_batch t WHERE o.id = t.id AND o.elevation_m IS NULL"
                )
            applied += len(batch)
        except psycopg.OperationalError as exc:  # lock_timeout / statement_timeout / transient
            stalled += len(batch)
            print(f"  ! batch of {len(batch)} stalled, left for re-run: {exc}")
        if sleep_s:
            time.sleep(sleep_s)
    return applied, stalled


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="download tiles + report, write nothing")
    parser.add_argument("--workers", type=int, default=12, help="concurrent tile downloads (default 12)")
    parser.add_argument("--batch-size", type=int, default=5000, help="rows per COPY + UPDATE batch (default 5000)")
    parser.add_argument(
        "--sleep", type=float, default=0.0, metavar="SECONDS", help="pause between write batches (default 0)"
    )
    parser.add_argument(
        "--max-cells",
        type=int,
        default=0,
        metavar="N",
        help="stop after N cells with missing rows (0 = all); for running prod in slices",
    )
    parser.add_argument(
        "--no-rebuild",
        dest="rebuild",
        action="store_false",
        help="skip the phenology rebuild afterward (region means then wait for the next ingest/refresh)",
    )
    args = parser.parse_args()

    cache = cache_dir()
    con = connect()

    cells = con.execute(
        f"SELECT DISTINCT floor(lat)::int, floor(lng)::int FROM observations WHERE {ELIGIBLE} ORDER BY 1, 2"
    ).fetchall()
    total_missing = (con.execute(f"SELECT count(*) FROM observations WHERE {ELIGIBLE}").fetchone() or (0,))[0]
    print(f"{total_missing:,} rows missing elevation across {len(cells)} tiles; cache {cache}")
    if not cells:
        return 0

    started = time.monotonic()
    tiles = {tile_id(south, west) for south, west in cells}
    counts = {"downloaded": 0, "cached": 0, "ocean": 0, "error": 0}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch_tile, cache, tid) for tid in tiles]
        for done, future in enumerate(as_completed(futures), 1):
            tid, status = future.result()
            key = status if status in counts else "error"
            counts[key] += 1
            if key == "error":
                print(f"  ! {tid}: {status}")
            if done % 200 == 0 or done == len(tiles):
                print(f"  tiles {done}/{len(tiles)}  {counts}")
    cache_gb = sum(p.stat().st_size for p in cache.glob("*.tif")) / 1e9
    print(f"tiles ready in {time.monotonic() - started:.0f}s ({cache_gb:.1f} GB cached)")
    if counts["error"]:
        print(f"WARNING: {counts['error']} tiles failed to fetch - re-run to retry them")

    if not args.dry_run:
        # Yield rather than queue behind the live server: a batch that can't get its lock in
        # 5s is left for a re-run (apply_updates counts it as stalled -> non-zero exit).
        con.execute("SET lock_timeout = '5s'")
        con.execute("SET statement_timeout = '120s'")

    started = time.monotonic()
    filled = no_value = stalled = 0
    processed_cells = 0
    for south, west in cells:
        tif = cache / f"{tile_id(south, west)}.tif"
        rows = con.execute(
            f"SELECT id, lat, lng FROM observations WHERE {ELIGIBLE} "
            "AND lat >= %s AND lat < %s AND lng >= %s AND lng < %s",
            [south, south + 1, west, west + 1],
        ).fetchall()
        if not rows or not tif.exists():
            no_value += len(rows)
            continue
        with rasterio.open(tif) as src:
            nodata = src.nodata
            updates: list[tuple[int, int]] = []
            samples = src.sample([(lng, lat) for _, lat, lng in rows])
            for (obs_id, _lat, _lng), sample in zip(rows, samples, strict=True):
                value = float(sample[0])
                if math.isnan(value) or (nodata is not None and value == nodata):
                    no_value += 1
                    continue
                updates.append((obs_id, round(value)))
        if not updates:
            continue
        processed_cells += 1
        if args.dry_run:
            filled += len(updates)
        else:
            applied, cell_stalled = apply_updates(con, updates, batch_size=args.batch_size, sleep_s=args.sleep)
            filled += applied
            stalled += cell_stalled
        if filled % 100_000 < len(updates):
            print(f"  filled {filled:,} / {total_missing:,}  ({time.monotonic() - started:.0f}s)")
        if args.max_cells and processed_cells >= args.max_cells:
            print(f"stopping after {processed_cells} cells (--max-cells); re-run for the rest")
            break

    print(f"{'DRY RUN - ' if args.dry_run else ''}filled {filled:,} rows; {no_value:,} had no DEM value (ocean/edge)")
    if stalled:
        print(f"WARNING: {stalled:,} rows stalled on a lock - re-run to finish them")
    if filled and args.rebuild and not args.dry_run:
        print("Rebuilding phenology so region elevation means pick up the new values…")
        build_phenology(con, Settings().cell_deg)
    remaining = (con.execute(f"SELECT count(*) FROM observations WHERE {ELIGIBLE}").fetchone() or (0,))[0]
    print(f"eligible rows still missing elevation: {remaining:,}")
    # Non-zero exit if any tile failed to fetch or any batch stalled, so the Ansible task (and
    # an operator) sees the run as incomplete and knows to re-run - what succeeded is already
    # written and a re-run only retries the rest.
    return 1 if counts["error"] or stalled else 0


if __name__ == "__main__":
    sys.exit(main())
