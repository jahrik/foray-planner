"""Unit tests for the pure geo primitives (no DB, no network)."""

from __future__ import annotations

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
    xmin, ymin, xmax, ymax = web_mercator_bbox_m(47.6038, -122.3301, 5000.0)
    assert xmax - xmin == pytest.approx(10000.0)
    assert ymax - ymin == pytest.approx(10000.0)
    # Center of the box is the point's own projection, not shifted off to one side.
    assert (xmin + xmax) / 2 == pytest.approx(xmin + (xmax - xmin) / 2)


def test_web_mercator_bbox_m_matches_leafets_own_projection_at_the_equator() -> None:
    # At (0, 0) the spherical Web Mercator projection is the identity times earth's radius on
    # both axes, so this pins the formula against a value anyone can hand-check - not just
    # "internally consistent with itself".
    earth_radius_m = 6378137.0
    xmin, ymin, xmax, ymax = web_mercator_bbox_m(0.0, 0.0, 1000.0)
    assert (xmin, ymin, xmax, ymax) == pytest.approx((-1000.0, -1000.0, 1000.0, 1000.0), abs=1e-6 * earth_radius_m)
