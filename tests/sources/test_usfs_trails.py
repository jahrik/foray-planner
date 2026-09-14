"""USFS Trail_NFS ingest tests - no network (mocked ArcGIS transport)."""

from __future__ import annotations

import json

import httpx
import psycopg
import pytest

from foray.cache import is_ingested, record_ingest
from foray.config import CoverageRegion, Settings
from foray.scoring import trails_near
from foray.sources.usfs_trails import (
    _USFS_TRAILS_VERSION,
    _attrs,
    _get,
    _parse_feature,
    _tracktype,
    fetch_usfs_trails,
    ingest_usfs_trails_coverage,
)

HOME_LAT, HOME_LNG = 41.35, -124.0


def _line(lat: float, lng: float, size: float = 0.01) -> dict:
    return {"type": "LineString", "coordinates": [[lng - size, lat - size], [lng + size, lat + size]]}


def test_get_is_case_insensitive() -> None:
    props = {"trail_name": "Lost Man Creek Trail", "TRAIL_CN": "123"}
    assert _get(props, "TRAIL_NAME") == "Lost Man Creek Trail"
    assert _get(props, "TRAIL_CN") == "123"
    assert _get(props, "missing") is None


def test_tracktype_inverts_usfs_trail_class_to_osm_grade() -> None:
    # USFS class 1 (minimal/undeveloped) is the roughest -> OSM's roughest grade5, and class 5
    # (fully developed) -> grade1 (best maintained) - opposite numbering scales.
    assert _tracktype(1) == "grade5"
    assert _tracktype(5) == "grade1"
    assert _tracktype(3) == "grade3"
    assert _tracktype(None) is None
    assert _tracktype("not a number") is None
    assert _tracktype(9) is None  # out of the 1-5 range


def test_attrs_keeps_the_minimal_field_subset() -> None:
    props = {
        "TRAIL_CLASS": 2,
        "TRAIL_SURFACE": "native",
        "MANAGING_ORG": "Six Rivers National Forest",
        "NATIONAL_TRAIL_DESIGNATION": "",
    }
    attrs = _attrs(props)
    assert attrs == {
        "trail_class": "2",
        "tracktype": "grade4",
        "trail_surface": "native",
        "managing_org": "Six Rivers National Forest",
    }


def test_attrs_returns_none_when_nothing_present() -> None:
    assert _attrs({}) is None


def test_parse_feature_builds_row_and_falls_back_on_missing_name() -> None:
    row = _parse_feature({"properties": {"TRAIL_CN": "42"}, "geometry": _line(HOME_LAT, HOME_LNG)})
    assert row is not None
    assert row[0] == "usfs:trail/42"
    assert row[1] == "USFS trail"  # name absent -> fallback
    assert row[2] == "path"
    assert row[3] == "usfs"
    assert json.loads(row[7])["type"] == "LineString"
    assert row[8] is None  # connects
    assert row[9] > 0  # recomputed length_km, not trusted from the source


def test_parse_feature_skips_missing_geometry_or_id() -> None:
    assert _parse_feature({"properties": {"TRAIL_CN": "1"}, "geometry": None}) is None
    assert _parse_feature({"properties": {}, "geometry": _line(HOME_LAT, HOME_LNG)}) is None


def test_fetch_usfs_trails_dedupes_by_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [
                    {"properties": {"TRAIL_CN": "7"}, "geometry": _line(HOME_LAT, HOME_LNG)},
                    {"properties": {"TRAIL_CN": "7"}, "geometry": _line(HOME_LAT, HOME_LNG)},
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_usfs_trails((-124.1, 41.3, -123.9, 41.4), client=client)
    assert [row[0] for row in rows] == ["usfs:trail/7"]


def test_fetch_usfs_trails_pages_until_transfer_limit_clears() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset == 0:
            features = [
                {"properties": {"TRAIL_CN": str(index)}, "geometry": _line(41.3 + index * 0.001, -124.0)}
                for index in range(1000)
            ]
            return httpx.Response(
                200, json={"type": "FeatureCollection", "features": features, "exceededTransferLimit": True}
            )
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"properties": {"TRAIL_CN": "1000"}, "geometry": _line(41.4, -124.0)}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_usfs_trails((-124.1, 41.3, -123.9, 41.4), client=client)
    ids = {row[0] for row in rows}
    assert "usfs:trail/1000" in ids
    assert len(ids) == 1001


def test_fetch_usfs_trails_keeps_parsed_rows_when_a_later_page_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(500)
        features = [
            {"properties": {"TRAIL_CN": str(index)}, "geometry": _line(41.3 + index * 0.001, -124.0)}
            for index in range(1000)
        ]
        return httpx.Response(
            200, json={"type": "FeatureCollection", "features": features, "exceededTransferLimit": True}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_usfs_trails((-124.1, 41.3, -123.9, 41.4), client=client)
    assert len(rows) == 1000  # best-effort: the first page's rows survive a later transport error


def test_ingest_usfs_trails_coverage_upserts_and_records_ingest(con: psycopg.Connection) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"properties": {"TRAIL_CN": "9"}, "geometry": _line(HOME_LAT, HOME_LNG)}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = Settings(coverage=[CoverageRegion(name="California", place_id=165, bbox=(-124.5, 32.5, -114.1, 42.1))])
    count = ingest_usfs_trails_coverage(cfg, con, client=client)
    assert count == 1
    assert is_ingested(con, f"usfs_trails:coverage:v{_USFS_TRAILS_VERSION}")
    # Second call skips before ever opening a client - if it didn't, this would try (and fail)
    # to reach the real ArcGIS service, since no client is passed here.
    assert ingest_usfs_trails_coverage(cfg, con) == 0

    rows = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0, kind="path")
    assert [trail.id for trail in rows] == ["usfs:trail/9"]
    assert rows[0].source == "usfs"


def test_ingest_usfs_trails_coverage_reruns_past_an_unversioned_marker(con: psycopg.Connection) -> None:
    record_ingest(con, "usfs_trails:coverage", 1)  # simulate a pre-versioned marker

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("resultOffset", "0"))
        if offset > 0:
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
        return httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [{"properties": {"TRAIL_CN": "1"}, "geometry": _line(HOME_LAT, HOME_LNG)}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = Settings(coverage=[CoverageRegion(name="California", place_id=165, bbox=(-124.5, 32.5, -114.1, 42.1))])
    assert ingest_usfs_trails_coverage(cfg, con, client=client) == 1
    assert is_ingested(con, f"usfs_trails:coverage:v{_USFS_TRAILS_VERSION}")


def test_ingest_usfs_trails_coverage_requires_a_coverage_bbox(con: psycopg.Connection) -> None:
    cfg = Settings(coverage=[CoverageRegion(name="No bbox", place_id=1)])
    with pytest.raises(ValueError, match="bbox"):
        ingest_usfs_trails_coverage(cfg, con)
