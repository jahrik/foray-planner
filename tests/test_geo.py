"""Unit tests for the pure geo primitives (no DB, no network)."""

from __future__ import annotations

import math

import pytest

from foray.geo import (
    bbox_around,
    bbox_around_segment,
    bbox_center_radius,
    grid_cell,
    grid_cells_in_bbox,
    haversine_km,
    web_mercator_bbox_m,
)

CELL = 0.25


def test_grid_cells_in_bbox_covers_the_home_disk() -> None:
    # Every cell whose center is within the radius must be in the enumerated candidate set -
    # the phenology ranking relies on this being a superset of the true disk.
    home_lat, home_lng = 44.06, -121.31
    radius_km = 60.0
    cells = set(grid_cells_in_bbox(bbox_around(home_lat, home_lng, radius_km), CELL))

    for dlat in range(-10, 11):
        for dlng in range(-10, 11):
            lat = home_lat + dlat * CELL / 2
            lng = home_lng + dlng * CELL / 2
            if haversine_km(home_lat, home_lng, lat, lng) <= radius_km:
                assert grid_cell(lat, lng, CELL).cell_id in cells


def test_grid_cells_in_bbox_ids_match_grid_cell() -> None:
    bbox = bbox_around(0.1, 0.1, 40.0)
    for cell_id in grid_cells_in_bbox(bbox, CELL):
        ilat, ilng = cell_id.split("_")
        clat, clng = (int(ilat) + 0.5) * CELL, (int(ilng) + 0.5) * CELL
        assert grid_cell(clat, clng, CELL).cell_id == cell_id


def test_bbox_around_segment_contains_both_endpoints_and_the_pad() -> None:
    box = bbox_around_segment(44.0, -121.0, 45.0, -122.0, 25.0)
    assert box.min_lat < 44.0 and box.max_lat > 45.0
    assert box.min_lng < -122.0 and box.max_lng > -121.0
    # ~25 km of latitude pad on each side (111 km/deg).
    assert box.min_lat == pytest.approx(44.0 - 25.0 / 111.0, abs=1e-6)


def test_bbox_center_radius_circle_contains_the_box() -> None:
    box = bbox_around_segment(44.0, -121.0, 45.6, -122.4, 40.0)
    clat, clng, radius_km = bbox_center_radius(box)
    # Sample the whole boundary (corners + edge points), not just the corners, so the test
    # pins "the circle contains the box" rather than a weaker four-point claim.
    lat_samples = [box.min_lat + (box.max_lat - box.min_lat) * f for f in (0.0, 0.25, 0.5, 0.75, 1.0)]
    lng_samples = [box.min_lng + (box.max_lng - box.min_lng) * f for f in (0.0, 0.25, 0.5, 0.75, 1.0)]
    for lat in lat_samples:
        for lng in lng_samples:
            assert haversine_km(clat, clng, lat, lng) <= radius_km + 1e-6


def test_grid_cells_in_bbox_along_corridor_includes_the_line() -> None:
    start = (44.0, -121.0)
    dest = (45.2, -122.4)
    cells = set(grid_cells_in_bbox(bbox_around_segment(*start, *dest, 20.0), CELL))
    # Sample points straight down the line - all must fall in an enumerated cell.
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        lat = start[0] + frac * (dest[0] - start[0])
        lng = start[1] + frac * (dest[1] - start[1])
        assert grid_cell(lat, lng, CELL).cell_id in cells


def test_web_mercator_bbox_m_is_a_square_centered_on_the_point() -> None:
    # radius_m=0 collapses the bbox to the point's own projection, independent of the
    # radius=5000 call below - a real fixed point to check centering against, not an arithmetic
    # identity that would pass even if the offset were applied unevenly.
    lat = 47.6038
    point_x, point_y, _, _ = web_mercator_bbox_m(lat, -122.3301, 0.0)
    xmin, ymin, xmax, ymax = web_mercator_bbox_m(lat, -122.3301, 5000.0)
    # Equal on both axes (square), but the projected-meter offset is the ground radius scaled by
    # the Web Mercator point scale sec(lat) - see the docstring for why the frontend circle's
    # getBounds() box needs that, not a bare ±radius_m.
    expected_side = 2 * 5000.0 / math.cos(math.radians(lat))
    assert xmax - xmin == pytest.approx(expected_side)
    assert ymax - ymin == pytest.approx(expected_side)
    assert (xmin + xmax) / 2 == pytest.approx(point_x)
    assert (ymin + ymax) / 2 == pytest.approx(point_y)


def test_web_mercator_bbox_m_clamps_latitude_to_the_valid_epsg3857_range() -> None:
    # Past ~85.0511 degrees the projection diverges to +-infinity - clamp instead of raising,
    # matching Leaflet's own CRS.EPSG3857 behavior, so an edge-of-grid region near a pole still
    # gets a finite bbox rather than crashing the satellite fetch.
    xmin, ymin, xmax, ymax = web_mercator_bbox_m(89.9, 0.0, 1000.0)
    clamped_xmin, clamped_ymin, clamped_xmax, clamped_ymax = web_mercator_bbox_m(85.0511287798, 0.0, 1000.0)
    assert (xmin, ymin, xmax, ymax) == pytest.approx((clamped_xmin, clamped_ymin, clamped_xmax, clamped_ymax))


def test_web_mercator_bbox_m_matches_leafets_own_projection_at_the_equator() -> None:
    # At (0, 0) the spherical Web Mercator projection is the identity times earth's radius on
    # both axes, so this pins the formula against a value anyone can hand-check - not just
    # "internally consistent with itself".
    earth_radius_m = 6378137.0
    xmin, ymin, xmax, ymax = web_mercator_bbox_m(0.0, 0.0, 1000.0)
    assert (xmin, ymin, xmax, ymax) == pytest.approx((-1000.0, -1000.0, 1000.0, 1000.0), abs=1e-6 * earth_radius_m)
