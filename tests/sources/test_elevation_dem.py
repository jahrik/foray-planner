"""Tile-id math and batch-write semantics for the steady-state DEM elevation backfill
(issue #334 PR 3, promoted from the one-off scripts/backfill_elevation_dem.py).

The Copernicus GLO-90 mirror keys each 1x1 degree tile on its south-west corner, so latitude
and longitude both floor toward negative infinity - a sign or padding slip here silently
samples the wrong cell (or a 404), so it is worth pinning.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

import foray.sources.elevation_dem as elevation_dem
from foray.sources.elevation_dem import _batched, apply_updates, backfill_elevation_dem, tile_id


@pytest.mark.parametrize(
    ("south", "west", "expected"),
    [
        (47, -123, "Copernicus_DSM_COG_30_N47_00_W123_00_DEM"),  # Pacific NW
        (0, 0, "Copernicus_DSM_COG_30_N00_00_E000_00_DEM"),  # Gulf of Guinea origin
        (-34, -59, "Copernicus_DSM_COG_30_S34_00_W059_00_DEM"),  # Buenos Aires - both hemispheres
        (52, 179, "Copernicus_DSM_COG_30_N52_00_E179_00_DEM"),  # western Aleutians, just west of the dateline
        (9, -1, "Copernicus_DSM_COG_30_N09_00_W001_00_DEM"),  # single-digit padding, W001 not W1
    ],
)
def test_tile_id_corners(south: int, west: int, expected: str) -> None:
    assert tile_id(south, west) == expected


_PAIRS = [(1, 10), (2, 20), (3, 30), (4, 40), (5, 50)]


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (2, [[(1, 10), (2, 20)], [(3, 30), (4, 40)], [(5, 50)]]),
        (5, [_PAIRS]),
        (0, [_PAIRS]),
    ],
)
def test_batched(size: int, expected: list[list[tuple[int, int]]]) -> None:
    assert [list(chunk) for chunk in _batched(_PAIRS, size)] == expected


def test_apply_updates_is_set_based_and_null_guarded(con: psycopg.Connection) -> None:
    """COPY + UPDATE ... FROM fills only the NULL rows, in batches, and never clobbers a value
    another writer (the Open-Meteo cron) already set."""
    con.execute("INSERT INTO observations (id, elevation_m) VALUES (1, NULL), (2, NULL), (3, NULL), (4, 777)")
    # ids 1-3 target NULL rows; id 4 is already set and must be left alone.
    applied, stalled = apply_updates(con, [(1, 100), (2, 200), (3, 300), (4, 999)], batch_size=2, sleep_s=0.0)
    assert (applied, stalled) == (4, 0)
    rows = dict(con.execute("SELECT id, elevation_m FROM observations ORDER BY id").fetchall())
    assert rows == {1: 100, 2: 200, 3: 300, 4: 777}


def test_backfill_elevation_dem_no_eligible_rows_is_a_noop(con: psycopg.Connection) -> None:
    result = backfill_elevation_dem(con)
    assert (result.filled, result.no_value, result.remaining) == (0, 0, 0)
    assert result.ok


class _FakeRasterSource:
    """Stands in for `rasterio.open(tif)` - `.sample()` returns a fixed elevation for every
    coordinate it's asked for, so the test never touches a real GeoTIFF."""

    nodata = None

    def __enter__(self) -> _FakeRasterSource:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def sample(self, coords: list[tuple[float, float]]) -> list[list[float]]:
        return [[123.4] for _ in coords]


def test_backfill_elevation_dem_fills_eligible_rows(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    con.execute(
        "INSERT INTO observations (id, lat, lng, quality_grade, obscured, elevation_m) VALUES "
        "(1, 47.6, -122.3, 'research', false, NULL), "
        "(2, 47.6, -122.3, 'needs_id', false, NULL)"  # not research-grade - must stay untouched
    )
    monkeypatch.setattr(elevation_dem, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(elevation_dem, "fetch_tile", lambda cache, tid: (tid, "cached"))
    # A fetch_tile returning "cached" still needs the file to actually exist for the
    # `tif.exists()` check before rasterio.open is reached.
    (tmp_path / f"{elevation_dem.tile_id(47, -123)}.tif").touch()
    monkeypatch.setattr(elevation_dem.rasterio, "open", lambda tif: _FakeRasterSource())

    result = backfill_elevation_dem(con)

    assert result.filled == 1
    assert result.ok
    row = con.execute("SELECT elevation_m FROM observations WHERE id = 1").fetchone()
    assert row == (123,)
    untouched = con.execute("SELECT elevation_m FROM observations WHERE id = 2").fetchone()
    assert untouched == (None,)


def test_backfill_elevation_dem_dry_run_writes_nothing(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    con.execute(
        "INSERT INTO observations (id, lat, lng, quality_grade, obscured, elevation_m) VALUES "
        "(1, 47.6, -122.3, 'research', false, NULL)"
    )
    monkeypatch.setattr(elevation_dem, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(elevation_dem, "fetch_tile", lambda cache, tid: (tid, "cached"))
    (tmp_path / f"{elevation_dem.tile_id(47, -123)}.tif").touch()
    monkeypatch.setattr(elevation_dem.rasterio, "open", lambda tif: _FakeRasterSource())

    result = backfill_elevation_dem(con, dry_run=True)

    assert result.filled == 1  # counted, but nothing written
    assert con.execute("SELECT elevation_m FROM observations WHERE id = 1").fetchone() == (None,)
