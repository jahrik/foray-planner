"""Tile-id math for the one-off DEM elevation backfill (scripts/backfill_elevation_dem.py).

The Copernicus GLO-90 mirror keys each 1x1 degree tile on its south-west corner, so latitude
and longitude both floor toward negative infinity - a sign or padding slip here silently
samples the wrong cell (or a 404), so it is worth pinning.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "backfill_elevation_dem", Path(__file__).parent.parent / "scripts" / "backfill_elevation_dem.py"
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
tile_id = _mod.tile_id


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
