"""Public-land ingest + scoring tests - no network (mocked ArcGIS transport)."""

from __future__ import annotations

import json

import httpx
import psycopg
import pytest

from foray.cache import is_ingested, upsert_public_land
from foray.config import CoverageRegion, Settings
from foray.land import (
    LandSource,
    _bounds,
    _coverage_envelope,
    _envelope,
    _get,
    _parse_feature,
    fetch_public_land,
    ingest_public_land_coverage,
)
from foray.scoring import land_near

HOME_LAT, HOME_LNG = 47.6, -122.3

BLM = LandSource(
    key="blm",
    agency="BLM",
    query_url="https://example.test/blm/query",
    where="ADMIN_AGENCY_CODE='BLM'",
    name_field="ADMIN_UNIT_NAME",
    fallback_name="BLM land",
)
USFS = LandSource(
    key="usfs",
    agency="USFS",
    query_url="https://example.test/usfs/query",
    where="1=1",
    name_field="FORESTNAME",
    fallback_name="National Forest",
)


def _polygon(lat: float, lng: float, size: float = 0.1) -> dict:
    """A small square GeoJSON polygon centered near (lat, lng)."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lng - size, lat - size],
                [lng + size, lat - size],
                [lng + size, lat + size],
                [lng - size, lat + size],
                [lng - size, lat - size],
            ]
        ],
    }


def test_get_is_case_insensitive() -> None:
    # ArcGIS geojson lowercases requested field names, so lookups must not be case-sensitive.
    props = {"forestname": "Gifford Pinchot National Forest", "OBJECTID": 5}
    assert _get(props, "FORESTNAME") == "Gifford Pinchot National Forest"
    assert _get(props, "OBJECTID") == 5
    assert _get(props, "missing") is None


def test_bounds_walks_nested_multipolygon_coords() -> None:
    multipolygon = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[-122.5, 47.5], [-122.0, 47.5], [-122.0, 48.0], [-122.5, 48.0], [-122.5, 47.5]]],
            [[[-123.0, 47.0], [-122.8, 47.0], [-122.8, 47.2], [-123.0, 47.2], [-123.0, 47.0]]],
        ],
    }
    assert _bounds(multipolygon["coordinates"]) == (-123.0, 47.0, -122.0, 48.0)
    assert _bounds([]) is None


def test_envelope_encloses_the_home_disk() -> None:
    xmin, ymin, xmax, ymax = _envelope(HOME_LAT, HOME_LNG, radius_km=50.0)
    assert ymin < HOME_LAT < ymax
    assert xmin < HOME_LNG < xmax
    # Longitude degrees are shorter than latitude at this latitude → wider lng span.
    assert (xmax - xmin) > (ymax - ymin)


def test_parse_feature_builds_row_and_falls_back_on_missing_name() -> None:
    row = _parse_feature(
        BLM,
        {"properties": {"OBJECTID": 42}, "geometry": _polygon(47.7, -122.1)},
    )
    assert row is not None
    assert row[0] == "blm:42"
    assert row[1] == "BLM"
    assert row[2] == "BLM land"  # name absent → fallback
    assert row[3] == "blm"
    assert row[4] == "https://example.test/blm"  # /query stripped
    # bbox: (id, agency, unit, source, url, min_lat, min_lng, max_lat, max_lng, geojson)
    assert row[5] == pytest.approx(47.6) and row[7] == pytest.approx(47.8)
    assert json.loads(row[9])["type"] == "Polygon"


def test_parse_feature_skips_missing_geometry_or_id() -> None:
    assert _parse_feature(BLM, {"properties": {"OBJECTID": 1}, "geometry": None}) is None
    assert _parse_feature(BLM, {"properties": {}, "geometry": _polygon(47.6, -122.3)}) is None


def test_fetch_public_land_dedupes_and_skips_a_failing_source() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "usfs" in str(request.url):
            return httpx.Response(500)  # this source is down → must be skipped, not fatal
        # Same BLM feature returned for every page-0 request; page-1 is empty → terminates.
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {"properties": {"OBJECTID": 7}, "geometry": _polygon(47.65, -122.35)},
                    {"properties": {"OBJECTID": 7}, "geometry": _polygon(47.65, -122.35)},
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_public_land(
        lat=HOME_LAT,
        lng=HOME_LNG,
        radius_km=50.0,
        client=client,
        sources=(BLM, USFS),
    )
    assert [row[0] for row in rows] == ["blm:7"]  # deduped; USFS 500 skipped


def test_fetch_public_land_skips_a_source_returning_malformed_payload() -> None:
    # A 200 that isn't well-formed GeoJSON (decode error) must be skipped like a transport
    # error - ownership ingest is best-effort and must not abort the refresh.
    def handler(request: httpx.Request) -> httpx.Response:
        if "usfs" in str(request.url):
            return httpx.Response(200, text="<html>maintenance</html>")  # not JSON
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"properties": {"OBJECTID": 3}, "geometry": _polygon(47.6, -122.3)}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_public_land(lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, client=client, sources=(BLM, USFS))
    assert [row[0] for row in rows] == ["blm:3"]  # BLM ingested; malformed USFS skipped


def test_fetch_public_land_pages_until_transfer_limit_clears() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset == 0:
            features = [
                {
                    "properties": {"OBJECTID": index},
                    "geometry": _polygon(47.6 + index * 0.001, -122.3),
                }
                for index in range(1000)  # a full page → exceededTransferLimit
            ]
            return httpx.Response(
                200,
                json={
                    "type": "FeatureCollection",
                    "features": features,
                    "exceededTransferLimit": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"properties": {"OBJECTID": 1000}, "geometry": _polygon(47.7, -122.3)}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_public_land(lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, client=client, sources=(BLM,))
    ids = {row[0] for row in rows}
    assert "blm:1000" in ids  # the second page was fetched
    assert len(ids) == 1001


def test_land_near_filters_by_bbox_and_returns_geometry(con: psycopg.Connection) -> None:
    near = _parse_feature(BLM, {"properties": {"OBJECTID": 1}, "geometry": _polygon(47.65, -122.35)})
    far = _parse_feature(
        USFS,
        {
            "properties": {"OBJECTID": 2, "FORESTNAME": "Faraway"},
            "geometry": _polygon(40.0, -122.0),
        },
    )
    assert near is not None and far is not None
    upsert_public_land(con, [near, far])

    units = land_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0)
    assert [unit.id for unit in units] == ["blm:1"]  # the 800 km-away unit's bbox is out of range
    assert units[0].agency == "BLM"
    assert units[0].geometry["type"] == "Polygon"  # geojson parsed back to a dict


def test_land_near_no_rows_ingested_returns_empty(con: psycopg.Connection) -> None:
    assert land_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0) == []


def test_coverage_envelope_unions_region_bboxes() -> None:
    regions = [
        CoverageRegion(name="A", place_id=1, bbox=(-124.0, 45.0, -120.0, 49.0)),
        CoverageRegion(name="B", place_id=2, bbox=(-121.0, 41.0, -116.0, 46.0)),
        CoverageRegion(name="No bbox", place_id=3),
    ]
    assert _coverage_envelope(regions) == (-124.0, 41.0, -116.0, 49.0)


def test_coverage_envelope_raises_when_nothing_has_a_bbox() -> None:
    with pytest.raises(ValueError, match="bbox"):
        _coverage_envelope([CoverageRegion(name="No bbox", place_id=1)])


def test_ingest_public_land_coverage_upserts_and_records_ingest(con: psycopg.Connection) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "usfs" in str(request.url):
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"properties": {"OBJECTID": 9}, "geometry": _polygon(47.6, -122.3)}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = Settings(coverage=[CoverageRegion(name="Washington", place_id=46, bbox=(-124.8, 45.5, -116.9, 49.0))])
    count = ingest_public_land_coverage(cfg, con, client=client, sources=(BLM, USFS))
    assert count == 1
    assert is_ingested(con, "land:coverage")
    # Second call skips before ever opening a client - if it didn't, this would try (and fail)
    # to reach the real ArcGIS services, since no client is passed here.
    assert ingest_public_land_coverage(cfg, con, sources=(BLM, USFS)) == 0
