"""Public-land parcel pins (issue #311): ``land_near`` + a parcel's entrance via ``resolve_pin``.

Hermetic hand-built polygons and lines - no network.
"""

from __future__ import annotations

import json

import psycopg
import pytest

from foray.cache import upsert_public_land, upsert_trails
from foray.scoring import land_near, resolve_pin

# A 0.1-degree square of National Forest east of the reference point, a tribal square right on
# top of it (cached for the map, never pinnable), and a BLM square well north with no trails.
WEST, EAST, SOUTH, NORTH = -121.0, -120.9, 45.0, 45.1
REF = (45.05, -121.2)  # ~16 km west of the forest's western edge


def _square(west: float, south: float, east: float, north: float) -> str:
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return json.dumps({"type": "Polygon", "coordinates": [ring]})


def _line(*points: tuple[float, float]) -> str:
    return json.dumps({"type": "LineString", "coordinates": [[lng, lat] for lat, lng in points]})


def _point(lat: float, lng: float) -> str:
    return json.dumps({"type": "Point", "coordinates": [lng, lat]})


@pytest.fixture(autouse=True)
def _seed(con: psycopg.Connection) -> None:
    upsert_public_land(
        con,
        [
            (
                "usfs:1",
                "USFS",
                "Test National Forest",
                "usfs",
                "https://example.test/usfs",
                _square(WEST, SOUTH, EAST, NORTH),
            ),
            (
                "tribal:1",
                "Tribal",
                "Test Reservation",
                "tribal",
                "https://example.test/tribal",
                _square(-121.15, 45.0, -121.05, 45.1),
            ),
            ("blm:1", "BLM", None, "blm", "https://example.test/blm", _square(-121.3, 45.4, -121.2, 45.5)),
        ],
    )


def test_land_near_lists_pinnable_parcels_nearest_first(con: psycopg.Connection) -> None:
    parcels = land_near(con, lat=REF[0], lng=REF[1], radius_km=60)
    assert [parcel.id for parcel in parcels] == ["usfs:1", "blm:1"]  # tribal land is not offered
    assert parcels[0].distance_km == pytest.approx(15.7, abs=0.5)
    assert parcels[0].url == "https://example.test/usfs"


def test_land_near_reports_zero_inside_a_parcel(con: psycopg.Connection) -> None:
    (inside,) = land_near(con, lat=45.05, lng=-120.95, radius_km=1)
    assert inside.id == "usfs:1"
    assert inside.distance_km == 0


def test_entrance_is_where_a_forest_road_crosses_into_the_parcel(con: psycopg.Connection) -> None:
    upsert_trails(
        con,
        [
            # Runs east from the reference point, entering the forest at its western edge.
            ("osm:way/1", "NF-10", "road", "osm", "u", 45.05, -121.1, _line(REF, (45.05, -120.95)), None, 25.0, None),
        ],
    )
    pin = resolve_pin(con, "land", "usfs:1", near=REF)
    assert pin is not None
    assert (pin.kind, pin.name, pin.feature_kind) == ("land", "Test National Forest", "road")
    assert (pin.lat, pin.lng) == pytest.approx((45.05, WEST), abs=1e-6)


def test_entrance_prefers_a_trailhead_over_a_nearer_path(con: psycopg.Connection) -> None:
    upsert_trails(
        con,
        [
            # A path hugging the western edge (nearer) and a trailhead deeper in (farther).
            (
                "osm:way/2",
                "Edge Path",
                "path",
                "osm",
                "u",
                45.05,
                -120.99,
                _line((45.02, -120.99), (45.08, -120.99)),
                None,
                6.7,
                None,
            ),
            (
                "osm:node/3",
                "Deep TH",
                "trailhead",
                "osm",
                "u",
                45.05,
                -120.92,
                _point(45.05, -120.92),
                None,
                None,
                None,
            ),
        ],
    )
    pin = resolve_pin(con, "land", "usfs:1", near=REF)
    assert pin is not None
    assert pin.feature_kind == "trailhead"
    assert (pin.lat, pin.lng) == pytest.approx((45.05, -120.92), abs=1e-6)


def test_entrance_falls_back_to_the_nearest_edge(con: psycopg.Connection) -> None:
    # No cached road/trail reaches the BLM square - the pin is its nearest boundary point.
    pin = resolve_pin(con, "land", "blm:1", near=REF)
    assert pin is not None
    assert (pin.name, pin.feature_kind) == ("BLM", "edge")
    assert (pin.lat, pin.lng) == pytest.approx((45.4, -121.2), abs=1e-6)


def test_unpinnable_or_missing_parcel_resolves_to_none(con: psycopg.Connection) -> None:
    assert resolve_pin(con, "land", "tribal:1", near=REF) is None
    assert resolve_pin(con, "land", "usfs:404", near=REF) is None


def test_land_pin_needs_a_reference_point(con: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="near"):
        resolve_pin(con, "land", "usfs:1")


def test_entrance_fallback_from_inside_is_the_nearest_boundary_point(con: psycopg.Connection) -> None:
    # Reference point inside the forest, 0.01 deg from its north edge, no cached features:
    # the pin must land on that edge, not stay at the interior reference point.
    pin = resolve_pin(con, "land", "usfs:1", near=(NORTH - 0.01, -120.95))
    assert pin is not None
    assert pin.feature_kind == "edge"
    assert (pin.lat, pin.lng) == pytest.approx((NORTH, -120.95), abs=1e-6)


def test_state_land_managers_are_pinnable(con: psycopg.Connection) -> None:
    upsert_public_land(
        con,
        [
            (
                "padus:slb1",
                "State Land Board",
                "Trust Lands",
                "padus",
                "https://example.test/slb",
                _square(-121.3, 45.0, -121.25, 45.05),
            ),
            (
                "padus:cty1",
                "County Land",
                "County Park",
                "padus",
                "https://example.test/cty",
                _square(-121.3, 45.06, -121.25, 45.1),
            ),
        ],
    )
    ids = {parcel.id for parcel in land_near(con, lat=REF[0], lng=REF[1], radius_km=10)}
    assert "padus:slb1" in ids
    assert "padus:cty1" not in ids  # local government land stays map-only
