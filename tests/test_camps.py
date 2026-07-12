"""Campground ingest + scoring tests — no network (mocked RIDB transport)."""

from __future__ import annotations

import duckdb
import httpx
import pytest

from foray.cache import SCHEMA, upsert_campsites
from foray.camps import (
    _clean_text,
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


def test_clean_text_strips_html_and_entities() -> None:
    assert _clean_text("<p>Extra Vehicle Fee $8.00</p>") == "Extra Vehicle Fee $8.00"
    assert _clean_text("Fees vary&nbsp;by&nbsp;season") == "Fees vary by season"
    assert _clean_text("<br/>") is None  # tags-only collapses to empty -> None
    assert _clean_text(None) is None


def test_parse_facility_cleans_html_fee_and_keeps_free_signal() -> None:
    row = _parse_facility(
        {
            "FacilityID": "9",
            "FacilityLatitude": 47.7,
            "FacilityLongitude": -122.1,
            "FacilityUseFeeDescription": "<p>No fee for this site</p>",
        }
    )
    assert row is not None
    assert row[3] == "No fee for this site"  # fee: HTML stripped
    assert row[4] is True  # free signal survives the cleaning


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
        lat=HOME_LAT,
        lng=HOME_LNG,
        radius_km=50.0,
        api_key="test-key",
        client=client,
        min_interval=0.0,
    )
    ids = [row[0] for row in rows]
    assert ids == ["ridb:1"]  # far one clipped out, near one deduped to a single row


def test_fetch_campsites_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("foray.camps.time.sleep", lambda _seconds: None)  # no real backoff wait
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:  # first request is rate-limited, then it succeeds on retry
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(
            200,
            json={
                "RECDATA": [
                    {
                        "FacilityID": "1",
                        "FacilityName": "CG",
                        "FacilityLatitude": 47.61,
                        "FacilityLongitude": -122.31,
                    }
                ],
                "METADATA": {"RESULTS": {"TOTAL_COUNT": 1}},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_campsites(
        lat=HOME_LAT,
        lng=HOME_LNG,
        radius_km=5.0,
        api_key="test-key",
        client=client,
        min_interval=0.0,
    )
    # Getting a row back at all proves the 429 was retried, not raised (which would abort
    # the whole ingest); the extra call is that retry.
    assert calls["n"] >= 2
    assert [row[0] for row in rows] == ["ridb:1"]


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
