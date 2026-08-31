#!/usr/bin/env python3
"""One-off: backfill `observations.elevation_m` from local Copernicus GLO-90 DEM tiles.

The hourly `foray-backfill-elevation` cron drains the backlog through Open-Meteo's free
elevation API, but that tier caps at ~10k points/day - against a multi-million-row backlog
that is ~200 days of trickle. This script samples the *same* DEM (Copernicus GLO-90, ~90 m,
nearest-cell - so values stay consistent with rows already enriched via `foray.elevation`)
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

Usage: `make ansible-backfill-elevation-dem-once` (prod), or
`uv run --with rasterio python scripts/backfill_elevation_dem.py [--dry-run] [--no-rebuild]`.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import rasterio

from foray.cache import connect
from foray.config import Settings
from foray.scoring import build_phenology

TILE_BUCKET = "https://copernicus-dem-90m.s3.amazonaws.com"
TILE_PREFIX = "Copernicus_DSM_COG_30"
DOWNLOAD_TIMEOUT_S = 180.0

# Only research-grade, unobscured, in-range rows - the same filter scoring reads (obscured
# points carry iNat's randomized decoy coordinate, so their elevation would be meaningless).
ELIGIBLE = (
    "elevation_m IS NULL AND quality_grade = 'research' AND NOT COALESCE(obscured, false) "
    "AND lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180"
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
    try:
        resp = httpx.get(f"{TILE_BUCKET}/{tid}/{tid}.tif", timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True)
    except httpx.HTTPError as error:
        return tid, f"error: {error}"
    if resp.status_code == 404:
        (cache / f"{tid}.missing").touch()
        return tid, "ocean"
    if resp.status_code != 200:
        return tid, f"http {resp.status_code}"
    tmp = tif.with_suffix(".part")
    tmp.write_bytes(resp.content)
    tmp.replace(tif)
    return tid, "downloaded"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="download tiles + report, write nothing")
    parser.add_argument("--workers", type=int, default=12, help="concurrent tile downloads (default 12)")
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

    started = time.monotonic()
    filled = no_value = 0
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
                updates.append((round(value), obs_id))
        if updates and not args.dry_run:
            with con.cursor() as cur:
                cur.executemany("UPDATE observations SET elevation_m = %s WHERE id = %s", updates)
        filled += len(updates)
        if filled % 100_000 < len(updates):
            print(f"  filled {filled:,} / {total_missing:,}  ({time.monotonic() - started:.0f}s)")

    print(f"{'DRY RUN - ' if args.dry_run else ''}filled {filled:,} rows; {no_value:,} had no DEM value (ocean/edge)")
    if filled and args.rebuild and not args.dry_run:
        print("Rebuilding phenology so region elevation means pick up the new values…")
        build_phenology(con, Settings().cell_deg)
    remaining = (con.execute(f"SELECT count(*) FROM observations WHERE {ELIGIBLE}").fetchone() or (0,))[0]
    print(f"eligible rows still missing elevation: {remaining:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
