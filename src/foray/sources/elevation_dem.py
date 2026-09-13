"""Steady-state `observations.elevation_m` backfill from local Copernicus GLO-90 DEM tiles
(issue #334 PR 3 - promoted from the one-off `scripts/backfill_elevation_dem.py`, issue #236).

The Open-Meteo-backed hourly cron (`foray.sources.elevation`) drains the backlog through a
free tier capped at ~10k points/day - against a multi-million-row backlog that was ~200 days
of trickle (see `project_dem_backfill_prod_too_slow`). This module samples the *same* DEM
(Copernicus GLO-90, ~90 m, nearest-cell - so values stay consistent with rows already enriched
via Open-Meteo) from 1x1 degree Cloud-Optimized GeoTIFF tiles instead, pulled from the public
AWS Open Data mirror (`s3://copernicus-dem-90m`, no credentials) into a local cache.

Was a one-shot ansible task (`infra/ansible/tasks/deploy/backfill_elevation_dem_once.yml`,
now retired) run once to clear the historical backlog. As a scheduled `foray` job
(`backfill-elevation-dem` in jobs.yaml) it now also keeps the *steady-state* trickle of newly-
ingested rows off the Open-Meteo free tier entirely - the daily volume of new eligible rows is
small enough that a fresh, uncached run (no persistent tile-cache volume needed for a scheduled
container - see jobs.yaml's comment) only touches whatever few cells today's new rows fall in,
not the whole historical footprint the original bulk clear needed.

Idempotent and resumable: only rows where `elevation_m IS NULL` are touched, and cached tiles
plus `.missing` markers (for the all-ocean cells the mirror does not publish) are reused within
one run.

Writes are set-based: each cell's sampled values go through a `COPY` into a TEMP table and a
single `UPDATE ... FROM`, chunked at `batch_size`, with `synchronous_commit = off` and a short
`lock_timeout` on the session. A naive `executemany` of ~1.8M single-row UPDATEs runs at
~180 rows/s against a network-attached managed Postgres and starves the live server of locks
and WAL bandwidth (it took prod's `/api/destinations` down once, see git history); this stays
out of its way.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg
import rasterio

logger = logging.getLogger(__name__)

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


@dataclass
class DemBackfillResult:
    filled: int
    no_value: int
    stalled: int
    tiles_downloaded: int
    tiles_cached: int
    tiles_ocean: int
    tiles_failed: int
    remaining: int

    @property
    def ok(self) -> bool:
        """False if a re-run is needed to finish the job (a tile failed to fetch, or a batch
        stalled on a lock) - mirrors the original script's non-zero exit code convention."""
        return not self.tiles_failed and not self.stalled


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
                # AND elevation_m IS NULL: never clobber a value the Open-Meteo cron may have
                # written into this cell since the rows were selected.
                cur.execute(
                    "UPDATE observations o SET elevation_m = t.elevation_m "
                    "FROM _elev_batch t WHERE o.id = t.id AND o.elevation_m IS NULL"
                )
            applied += len(batch)
        except psycopg.OperationalError as exc:  # lock_timeout / statement_timeout / transient
            stalled += len(batch)
            logger.warning("elevation_dem: batch of %d stalled, left for re-run: %s", len(batch), exc)
        if sleep_s:
            time.sleep(sleep_s)
    return applied, stalled


def backfill_elevation_dem(
    con: psycopg.Connection,
    *,
    dry_run: bool = False,
    workers: int = 12,
    batch_size: int = 5000,
    sleep_s: float = 0.0,
    max_cells: int = 0,
) -> DemBackfillResult:
    """Sample local GLO-90 tiles for every eligible row missing elevation, writing set-based.
    ``max_cells`` (0 = all) caps how many 1x1 degree cells this call processes, for running a
    single invocation in slices. Downloads whatever tiles aren't already cached under
    ``FORAY_DEM_CACHE`` (or the platform cache dir default) first, with ``workers`` concurrent
    fetches."""
    cache = cache_dir()
    cells = con.execute(
        f"SELECT DISTINCT floor(lat)::int, floor(lng)::int FROM observations WHERE {ELIGIBLE} ORDER BY 1, 2"
    ).fetchall()
    total_missing = (con.execute(f"SELECT count(*) FROM observations WHERE {ELIGIBLE}").fetchone() or (0,))[0]
    logger.info("elevation_dem: %d rows missing elevation across %d tiles; cache %s", total_missing, len(cells), cache)
    if not cells:
        return DemBackfillResult(0, 0, 0, 0, 0, 0, 0, 0)

    started = time.monotonic()
    tiles = {tile_id(south, west) for south, west in cells}
    counts = {"downloaded": 0, "cached": 0, "ocean": 0, "error": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_tile, cache, tid) for tid in tiles]
        for future in as_completed(futures):
            tid, status = future.result()
            key = status if status in counts else "error"
            counts[key] += 1
            if key == "error":
                logger.warning("elevation_dem: tile %s: %s", tid, status)
    logger.info(
        "elevation_dem: tiles ready in %.0fs (%s)",
        time.monotonic() - started,
        counts,
    )

    if not dry_run:
        # Yield rather than queue behind the live server: a batch that can't get its lock in
        # 5s is left for a re-run (apply_updates counts it as stalled).
        con.execute("SET lock_timeout = '5s'")
        con.execute("SET statement_timeout = '120s'")

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
        if dry_run:
            filled += len(updates)
        else:
            applied, cell_stalled = apply_updates(con, updates, batch_size=batch_size, sleep_s=sleep_s)
            filled += applied
            stalled += cell_stalled
        if max_cells and processed_cells >= max_cells:
            logger.info("elevation_dem: stopping after %d cells (max_cells) - re-run for the rest", processed_cells)
            break

    remaining = (con.execute(f"SELECT count(*) FROM observations WHERE {ELIGIBLE}").fetchone() or (0,))[0]
    logger.info(
        "elevation_dem: %sfilled %d rows; %d had no DEM value; %d eligible rows still missing elevation",
        "DRY RUN - " if dry_run else "",
        filled,
        no_value,
        remaining,
    )
    return DemBackfillResult(
        filled=filled,
        no_value=no_value,
        stalled=stalled,
        tiles_downloaded=counts["downloaded"],
        tiles_cached=counts["cached"],
        tiles_ocean=counts["ocean"],
        tiles_failed=counts["error"],
        remaining=remaining,
    )
