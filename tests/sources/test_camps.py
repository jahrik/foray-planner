"""Campground ingest + scoring tests - no network (mocked RIDB transport)."""

from __future__ import annotations

import httpx
import psycopg
import pytest

from foray.cache import prune_campsites_outside_radius, upsert_campsites
from foray.config import CoverageRegion
from foray.scoring import camps_near
from foray.sources.camps import (
    _clean_text,
    _fee_range,
    _free_from_fee,
    _parse_facility,
    _query_centers,
    _states_for_disk,
    fetch_campsites,
)

HOME_LAT, HOME_LNG = 47.6, -122.3


def test_free_from_fee_only_asserts_on_explicit_signal() -> None:
    assert _free_from_fee("No fee for this site") is True
    assert _free_from_fee("$0.00 per night") is True
    assert _free_from_fee("Fee: $0 per night, $0 per extra vehicle") is True  # every amount is $0
    # A described fee, or silence, is left unknown - never guessed as paid or free.
    assert _free_from_fee("$15 per night") is None
    assert _free_from_fee(None) is None
    assert _free_from_fee("") is None


def test_fee_range_pulls_plausible_nightly_amounts() -> None:
    assert _fee_range("Camping Fees are $16/vehicle, $2 per extra vehicle") == (2.0, 16.0)
    assert _fee_range("$8 per single campsite; $16 per group site") == (8.0, 16.0)
    assert _fee_range("$20.00 per night") == (20.0, 20.0)
    assert _fee_range("Violations subject to a $500 fine") == (None, None)  # over the nightly cap
    assert _fee_range(None) == (None, None)


def test_parse_facility_reads_reservable_and_fee_range() -> None:
    row = _parse_facility(
        {
            "FacilityID": "77",
            "FacilityName": "Bedrock CG",
            "FacilityLatitude": 44.0,
            "FacilityLongitude": -122.5,
            "Reservable": True,
            "FacilityUseFeeDescription": "<p>$18 per night, $9 per extra vehicle</p>",
        }
    )
    assert row is not None
    assert row[9] is True  # reservable
    assert (row[10], row[11]) == (9.0, 18.0)  # fee_low, fee_high


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
    assert _parse_facility({"FacilityID": "1", "FacilityLatitude": 0.0, "FacilityLongitude": 0.0}) is None
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
    from foray.geo import haversine_km

    query_radius = 80.0
    small = _query_centers(HOME_LAT, HOME_LNG, radius_km=5.0, query_radius_km=query_radius)
    big = _query_centers(HOME_LAT, HOME_LNG, radius_km=300.0, query_radius_km=query_radius)
    # A wider disk needs strictly more query circles to cover it.
    assert len(big) > len(small)
    # Every center is near enough that its query circle can reach the home disk.
    for center_lat, center_lng in big:
        assert haversine_km(HOME_LAT, HOME_LNG, center_lat, center_lng) <= 300.0 + query_radius


def test_query_centers_caps_request_count_for_huge_radius() -> None:
    from foray.geo import haversine_km
    from foray.sources.camps import _MAX_QUERY_CENTERS

    # An uncapped grid at this radius would need tens of thousands of query circles.
    centers = _query_centers(HOME_LAT, HOME_LNG, radius_km=10000.0, query_radius_km=80.0)
    assert len(centers) == _MAX_QUERY_CENTERS
    # The kept centers are the ones closest to home, not an arbitrary slice.
    distances = [haversine_km(HOME_LAT, HOME_LNG, lat, lng) for lat, lng in centers]
    assert distances == sorted(distances)


_COVERAGE = [
    CoverageRegion(name="Oregon", place_id=10, bbox=(-124.57, 41.99, -116.46, 46.29)),
    CoverageRegion(name="Washington", place_id=11, bbox=(-124.85, 45.54, -116.92, 49.00)),
    CoverageRegion(name="California", place_id=12, bbox=(-124.48, 32.53, -114.13, 42.01)),
    CoverageRegion(name="Maine", place_id=13, bbox=(-71.08, 42.92, -66.88, 47.46)),
    CoverageRegion(name="United States", place_id=1),  # no bbox → skipped
]


def test_states_for_disk_picks_only_regions_the_disk_reaches() -> None:
    # ~300 km around Coos Bay, OR reaches OR + WA + northern CA, never Maine.
    codes = _states_for_disk(_COVERAGE, 43.37, -124.22, 300.0)
    assert set(codes) == {"OR", "WA", "CA"}
    # A tight disk stays inside one state.
    assert _states_for_disk(_COVERAGE, 44.0, -120.5, 20.0) == ["OR"]
    # A non-US point resolves nothing → caller falls back to radius tiling.
    assert _states_for_disk(_COVERAGE, 48.85, 2.35, 100.0) == []


def test_fetch_campsites_lists_by_state_and_clips_to_radius() -> None:
    seen_states: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_states.append(request.url.params["state"])
        assert request.url.params["activity"] == "CAMPING"
        return httpx.Response(
            200,
            json={
                "RECDATA": [
                    {
                        "FacilityID": "1",
                        "FacilityName": "Near",
                        "FacilityLatitude": 47.65,
                        "FacilityLongitude": -122.35,
                    },
                    {"FacilityID": "2", "FacilityName": "Far", "FacilityLatitude": 40.0, "FacilityLongitude": -122.3},
                ],
                "METADATA": {"RESULTS": {"TOTAL_COUNT": 2}},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_campsites(
        lat=HOME_LAT,
        lng=HOME_LNG,
        radius_km=50.0,
        api_key="test-key",
        states=["WA", "OR"],
        client=client,
        min_interval=0.0,
    )
    assert seen_states == ["WA", "OR"]  # one paged listing per state, no radius tiling
    assert [row[0] for row in rows] == ["ridb:1"]  # far one clipped, near one deduped across states


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
        "FacilityLatitude": 40.0,  # ~800 km south - outside the radius
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
    monkeypatch.setattr("foray.sources.camps.time.sleep", lambda _seconds: None)  # no real backoff wait
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


def test_camps_near_ranks_free_first_then_distance(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            # (id, name, kind, fee, free, lat, lng, source, url)
            ("ridb:1", "Paid Close", "campground", "$20", None, 47.61, -122.31, "ridb", "u1", None, None, None),
            ("ridb:2", "Free Far", "campground", "No fee", True, 47.9, -122.6, "ridb", "u2", None, None, None),
            ("ridb:3", "Free Close", "campground", "No fee", True, 47.62, -122.32, "ridb", "u3", None, None, None),
            ("ridb:4", "Way Out", "campground", None, None, 40.0, -122.0, "ridb", "u4", None, None, None),
        ],
    )
    sites = camps_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=100.0)
    names = [site.name for site in sites]
    # Free sites first (nearest free before farther free), then the paid one; the 800 km
    # facility is outside the radius.
    assert names == ["Free Close", "Free Far", "Paid Close"]

    free = camps_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=100.0, free_only=True)
    assert [site.name for site in free] == ["Free Close", "Free Far"]


def test_camps_near_ranks_by_true_distance_not_rounded(con: psycopg.Connection) -> None:
    # Two sites whose distances both round to 2.2 km but differ slightly. The farther one is
    # inserted first, so sorting by the *rounded* key (a tie) would leave it mis-ordered via a
    # stable sort; only sorting by true distance puts the nearer one first.
    upsert_campsites(
        con,
        [
            ("ridb:far", "Far", "campground", None, None, 47.6200, -122.30, "ridb", "u", None, None, None),
            ("ridb:near", "Near", "campground", None, None, 47.6199, -122.30, "ridb", "u", None, None, None),
        ],
    )
    sites = camps_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0)
    assert sites[0].distance_km == sites[1].distance_km == 2.2  # tie once rounded
    assert [site.name for site in sites] == ["Near", "Far"]  # ordered by true distance


def test_prune_campsites_outside_radius_drops_only_stale_ridb_rows(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            ("ridb:near", "Near", "campground", None, None, 47.61, -122.31, "ridb", "u", None, None, None),  # ~1 km
            (
                "ridb:far",
                "Far",
                "campground",
                None,
                None,
                40.0,
                -122.3,
                "ridb",
                "u",
                None,
                None,
                None,
            ),  # ~800 km - stale
            (
                "osm:x",
                "OSM Far",
                "reported",
                None,
                None,
                40.0,
                -122.3,
                "osm",
                "u",
                None,
                None,
                None,
            ),  # other source, untouched
        ],
    )
    deleted = prune_campsites_outside_radius(con, "ridb", 47.6, -122.3, 100.0)
    assert deleted == 1
    ids = {row[0] for row in con.execute("SELECT id FROM campsites").fetchall()}
    assert ids == {"ridb:near", "osm:x"}


def test_camps_near_no_rows_ingested_returns_empty(con: psycopg.Connection) -> None:
    assert camps_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0) == []


def test_camps_near_limit_caps_ranked_result(con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            ("ridb:1", "Paid Close", "campground", "$20", None, 47.61, -122.31, "ridb", "u1", None, None, None),
            ("ridb:2", "Free Far", "campground", "No fee", True, 47.9, -122.6, "ridb", "u2", None, None, None),
            ("ridb:3", "Free Close", "campground", "No fee", True, 47.62, -122.32, "ridb", "u3", None, None, None),
        ],
    )
    sites = camps_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=100.0, limit=2)
    # Same free-first-then-distance order as the unlimited result, just truncated to `limit`.
    assert [site.name for site in sites] == ["Free Close", "Free Far"]
