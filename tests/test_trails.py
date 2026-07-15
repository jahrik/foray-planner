"""Trail ingest + scoring tests - no network (mocked Overpass transport)."""

from __future__ import annotations

import json

import httpx
import psycopg
import pytest

from foray.cache import is_ingested, upsert_campsites, upsert_trails
from foray.config import Config, CoverageRegion, Home, Ingest
from foray.scoring import trails_near
from foray.trails import (
    _bbox_filter,
    _parse_element,
    _parse_trails,
    _sample,
    fetch_trails,
    ingest_trails,
    ingest_trails_region,
)

HOME_LAT, HOME_LNG = 47.6, -122.3


def test_parse_element_reads_a_path_way() -> None:
    element = {
        "type": "way",
        "id": 100,
        "tags": {"highway": "path", "name": "Ridge Trail"},
        "geometry": [{"lat": 47.6, "lon": -122.3}, {"lat": 47.62, "lon": -122.28}],
    }
    row = _parse_element(element)
    assert row is not None
    # (id, name, kind, source, url, min_lat, min_lng, max_lat, max_lng, center_lat, center_lng, geo)
    assert row[0] == "osm:way/100"
    assert row[1] == "Ridge Trail"
    assert row[2] == "path"
    assert row[3] == "osm"
    assert row[4] == "https://www.openstreetmap.org/way/100"
    assert row[5] == pytest.approx(47.6) and row[7] == pytest.approx(47.62)
    geometry = json.loads(row[11])
    assert geometry["type"] == "LineString"
    assert geometry["coordinates"][0] == [-122.3, 47.6]  # GeoJSON is [lng, lat]


def test_parse_element_names_unnamed_way_from_ref_then_fallback() -> None:
    ref_only = _parse_element(
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "path", "ref": "FR 100"},
            "geometry": [{"lat": 47.6, "lon": -122.3}, {"lat": 47.61, "lon": -122.29}],
        }
    )
    assert ref_only is not None and ref_only[1] == "FR 100"
    bare = _parse_element(
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "path"},
            "geometry": [{"lat": 47.6, "lon": -122.3}, {"lat": 47.61, "lon": -122.29}],
        }
    )
    assert bare is not None and bare[1] == "Trail (OSM)"


def test_parse_element_reads_a_trailhead_node() -> None:
    row = _parse_element({"type": "node", "id": 9, "lat": 47.6, "lon": -122.3, "tags": {"highway": "trailhead"}})
    assert row is not None
    assert row[0] == "osm:node/9"
    assert row[1] == "Trailhead (OSM)"  # unnamed → fallback
    assert row[2] == "trailhead"
    assert json.loads(row[11])["type"] == "Point"


def test_parse_element_stitches_a_hiking_route_relation() -> None:
    row = _parse_element(
        {
            "type": "relation",
            "id": 7,
            "tags": {"route": "hiking", "name": "PCT Section"},
            "members": [
                {
                    "type": "way",
                    "geometry": [{"lat": 47.6, "lon": -122.3}, {"lat": 47.61, "lon": -122.29}],
                },
                {  # a node member (e.g. a guidepost) has no line geometry → ignored
                    "type": "node",
                    "lat": 47.6,
                    "lon": -122.3,
                },
                {
                    "type": "way",
                    "geometry": [{"lat": 47.62, "lon": -122.28}, {"lat": 47.63, "lon": -122.27}],
                },
            ],
        }
    )
    assert row is not None
    assert row[0] == "osm:relation/7"
    assert row[2] == "route"
    geometry = json.loads(row[11])
    assert geometry["type"] == "MultiLineString"
    assert len(geometry["coordinates"]) == 2  # two way members stitched, node member dropped


def test_parse_element_skips_geometryless_way_and_relation() -> None:
    assert _parse_element({"type": "way", "id": 3, "tags": {"highway": "path"}}) is None
    assert _parse_element({"type": "relation", "id": 4, "members": []}) is None
    assert _parse_element({"type": "way", "tags": {}}) is None  # no id


def test_parse_trails_dedupes_by_id() -> None:
    node = {"type": "node", "id": 1, "lat": 47.6, "lon": -122.3, "tags": {"highway": "trailhead"}}
    payload = {"elements": [node, node]}
    assert [row[0] for row in _parse_trails(payload)] == ["osm:node/1"]


def test_sample_thins_to_cap_keeping_endpoints() -> None:
    coords = [(float(index), 0.0) for index in range(200)]
    thinned = _sample(coords, 60)
    assert len(thinned) == 60
    assert thinned[0] == coords[0] and thinned[-1] == coords[-1]
    assert _sample(coords[:10], 60) == coords[:10]  # under the cap → unchanged


def test_fetch_trails_skips_a_failing_query() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    assert fetch_trails(lat=HOME_LAT, lng=HOME_LNG, radius_km=40.0, client=client) == []


def test_fetch_trails_parses_a_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "way",
                        "id": 100,
                        "tags": {"highway": "path", "name": "Ridge Trail"},
                        "geometry": [
                            {"lat": 47.6, "lon": -122.3},
                            {"lat": 47.61, "lon": -122.29},
                        ],
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_trails(lat=HOME_LAT, lng=HOME_LNG, radius_km=40.0, client=client)
    assert [row[0] for row in rows] == ["osm:way/100"]


def test_trails_near_filters_by_radius_and_ranks_nearest_first(
    con: psycopg.Connection,
) -> None:
    near = _parse_element(
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "path", "name": "Near"},
            "geometry": [{"lat": 47.61, "lon": -122.31}, {"lat": 47.62, "lon": -122.30}],
        }
    )
    farther = _parse_element(
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "path", "name": "Farther"},
            "geometry": [{"lat": 47.7, "lon": -122.4}, {"lat": 47.71, "lon": -122.39}],
        }
    )
    out_of_range = _parse_element(
        {
            "type": "way",
            "id": 3,
            "tags": {"highway": "path", "name": "Faraway"},
            "geometry": [{"lat": 40.0, "lon": -120.0}, {"lat": 40.01, "lon": -120.01}],
        }
    )
    assert near is not None and farther is not None and out_of_range is not None
    upsert_trails(con, [near, farther, out_of_range])

    trails = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0)
    assert [trail.name for trail in trails] == ["Near", "Farther"]  # 800 km trail excluded
    assert trails[0].distance_km <= trails[1].distance_km  # nearest first
    assert trails[0].camp_distance_km is None  # no campsites cached yet


def test_trails_near_annotates_nearest_campsite(con: psycopg.Connection) -> None:
    trail = _parse_element(
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "path", "name": "Ridge"},
            "geometry": [{"lat": 47.6, "lon": -122.3}, {"lat": 47.61, "lon": -122.29}],
        }
    )
    assert trail is not None
    upsert_trails(con, [trail])
    upsert_campsites(
        con,
        [("ridb:1", "Camp", "campground", None, True, 47.6, -122.3, "ridb", "http://x")],
    )
    trails = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0)
    assert trails[0].camp_distance_km is not None
    assert trails[0].camp_distance_km < 5.0  # the campsite sits right on the trail's center


def test_trails_near_no_rows_ingested_returns_empty(con: psycopg.Connection) -> None:
    assert trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0) == []


def test_ingest_trails_upserts_into_cache(con: psycopg.Connection) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "way",
                        "id": 42,
                        "tags": {"highway": "path", "name": "Riverside Trail"},
                        "geometry": [
                            {"lat": 47.61, "lon": -122.31},
                            {"lat": 47.62, "lon": -122.30},
                        ],
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = Config(
        home=Home(name="Home", lat=HOME_LAT, lng=HOME_LNG, radius_km=40.0),
        cell_deg=0.5,
        ingest=Ingest(since_year=2015, quality_grade="research", recent_weeks=4),
    )
    count = ingest_trails(cfg, con, client=client)
    assert count == 1
    trails = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0)
    assert [trail.name for trail in trails] == ["Riverside Trail"]
    assert trails[0].kind == "path"


def test_bbox_filter_formats_south_west_north_east() -> None:
    assert _bbox_filter(45.5, -124.8, 49.0, -116.9) == "(45.5,-124.8,49.0,-116.9)"


def test_ingest_trails_region_requires_a_bbox() -> None:
    region = CoverageRegion(name="No Bbox", place_id=999)
    with pytest.raises(ValueError, match="bbox"):
        ingest_trails_region(region)


def test_ingest_trails_region_upserts_and_records_ingest(con: psycopg.Connection) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "way",
                        "id": 7,
                        "tags": {"highway": "path", "name": "State Trail"},
                        "geometry": [
                            {"lat": 47.61, "lon": -122.31},
                            {"lat": 47.62, "lon": -122.30},
                        ],
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    region = CoverageRegion(name="Washington", place_id=46, bbox=(-124.8, 45.5, -116.9, 49.0))
    count = ingest_trails_region(region, con, client=client)
    assert count == 1
    assert is_ingested(con, "trails:place:46")
    # Second call skips before ever opening a client - if it didn't, this would try (and fail)
    # to reach the real Overpass API, since no client is passed here.
    assert ingest_trails_region(region, con) == 0
