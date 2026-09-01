"""Tile-id math for the one-off DEM elevation backfill (scripts/backfill_elevation_dem.py).

The Copernicus GLO-90 mirror keys each 1x1 degree tile on its south-west corner, so latitude
and longitude both floor toward negative infinity - a sign or padding slip here silently
samples the wrong cell (or a 404), so it is worth pinning.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import psycopg
import pytest

_spec = importlib.util.spec_from_file_location(
    "backfill_elevation_dem", Path(__file__).parent.parent / "scripts" / "backfill_elevation_dem.py"
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
tile_id = _mod.tile_id
apply_updates = _mod.apply_updates


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


@pytest.mark.parametrize(
    ("size", "expected"),
    [(2, [[1, 2], [3, 4], [5]]), (5, [[1, 2, 3, 4, 5]]), (0, [[1, 2, 3, 4, 5]])],
)
def test_batched(size: int, expected: list[list[int]]) -> None:
    assert [list(chunk) for chunk in _mod._batched([1, 2, 3, 4, 5], size)] == expected


def test_apply_updates_is_set_based_and_null_guarded(con: psycopg.Connection) -> None:
    """COPY + UPDATE ... FROM fills only the NULL rows, in batches, and never clobbers a value
    another writer (the Open-Meteo cron) already set."""
    con.execute("INSERT INTO observations (id, elevation_m) VALUES (1, NULL), (2, NULL), (3, NULL), (4, 777)")
    # ids 1-3 target NULL rows; id 4 is already set and must be left alone.
    applied, stalled = apply_updates(con, [(1, 100), (2, 200), (3, 300), (4, 999)], batch_size=2, sleep_s=0.0)
    assert (applied, stalled) == (4, 0)
    rows = dict(con.execute("SELECT id, elevation_m FROM observations ORDER BY id").fetchall())
    assert rows == {1: 100, 2: 200, 3: 300, 4: 777}
