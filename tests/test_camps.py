"""Campground ingest + scoring tests — no network (mocked RIDB transport)."""

from __future__ import annotations

import duckdb
import httpx
import pytest

from foray.cache import SCHEMA, upsert_campsites
from foray.camps import (
    _free_from_fee,
    _parse_facility,
    _query_centers,
    fetch_campsites,
)
from foray.scoring import camps_near

HOME_LAT, HOME_LNG = 47.6, -122.3


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute(SCHEMA)
    return conn


def test_free_from_fee_only_asserts_on_explicit_signal() -> None:
    assert _free_from_fee("No fee for this site") is True
    assert _free_from_fee("$0.00 per night") is True
    # A described fee, or silence, is left unknown — never guessed as paid or free.
    assert _free_from_fee("$15 per night") is None
    assert _free_from_fee(None) is None
    assert _free_from_fee("") is None


def test_parse_facility_skips_missing_and_zero_coords() -> None:
    assert _parse_facility({"FacilityID": "1", "FacilityName": "x"}) is None
    assert (
        _parse_facility({"FacilityID": "1", "FacilityLatitude": 0.0, "FacilityLongitude": 0.0})
        is None
    )
    row = _parse_facility(
        {
            "FacilityID": "250018",
            "FacilityName": "Cool Creek CG",
            "FacilityLatitude": 47.7,
            "FacilityLongitude": -122.1,
            "FacilityUseFeeDescription": "No fee",
        }
    )
    assert row is not None
    assert row[0] == "ridb:250018"
    assert row[2] == "campground"
    assert row[4] is True  # free
    assert row[8] == "https://www.recreation.gov/camping/campgrounds/250018"


def test_query_centers_cover_the_disk() -> None:
    from foray.scoring import haversine_km

    query_radius = 80.0
    small = _query_centers(HOME_LAT, HOME_LNG, radius_km=5.0, query_radius_km=query_radius)
    big = _query_centers(HOME_LAT, HOME_LNG, radius_km=300.0, query_radius_km=query_radius)
    # A wider disk needs strictly more query circles to cover it.
    assert len(big) > len(small)
    # Every center is near enough that its query circle can reach the home disk.
    for center_lat, center_lng in big:
        assert haversine_km(HOME_LAT, HOME_LNG, center_lat, center_lng) <= 300.0 + query_radius


def test_fetch_campsites_dedupes_and_clips_to_radius() -> None:
    near = {
        "FacilityID": "1",
        "FacilityName": "Near CG",
        "FacilityLatitude": 47.65,
        "FacilityLongitude": -122.35,
    }
    far = {
        "FacilityID": "2",
        "FacilityName": "Far CG",
        "FacilityLatitude": 40.0,  # ~800 km south — outside the radius
        "FacilityLongitude": -122.3,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == "test-key"
        # Same two facilities returned for every query circle → dedup must collapse them.
        return httpx.Response(
            200,
            json={"RECDATA": [near, far], "METADATA": {"RESULTS": {"TOTAL_COUNT": 2}}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_campsites(
        lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, api_key="test-key", client=client
    )
    ids = [row[0] for row in rows]
    assert ids == ["ridb:1"]  # far one clipped out, near one deduped to a single row


def test_camps_near_ranks_free_first_then_distance(con: duckdb.DuckDBPyConnection) -> None:
    upsert_campsites(
        con,
        [
            # (id, name, kind, fee, free, lat, lng, source, url)
            ("ridb:1", "Paid Close", "campground", "$20", None, 47.61, -122.31, "ridb", "u1"),
            ("ridb:2", "Free Far", "campground", "No fee", True, 47.9, -122.6, "ridb", "u2"),
            ("ridb:3", "Free Close", "campground", "No fee", True, 47.62, -122.32, "ridb", "u3"),
            ("ridb:4", "Way Out", "campground", None, None, 40.0, -122.0, "ridb", "u4"),
        ],
    )
    sites = camps_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=100.0)
    names = [site.name for site in sites]
    # Free sites first (nearest free before farther free), then the paid one; the 800 km
    # facility is outside the radius.
    assert names == ["Free Close", "Free Far", "Paid Close"]

    free = camps_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=100.0, free_only=True)
    assert [site.name for site in free] == ["Free Close", "Free Far"]


def test_camps_near_missing_table_returns_empty() -> None:
    conn = duckdb.connect(":memory:")  # no schema → no campsites table
    assert camps_near(conn, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0) == []
