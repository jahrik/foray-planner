"""Trail ingest + scoring tests - no network (mocked Overpass transport)."""

from __future__ import annotations

import json

import httpx
import psycopg
import pytest

from foray.cache import is_ingested, upsert_campsites, upsert_trails
from foray.config import CoverageRegion, Home, Ingest, Settings
from foray.scoring import get_trail, nearest_trail, trails_near
from foray.sources.trails import (
    _network_query,
    _parse_element,
    _parse_trailhead_id,
    _parse_trails,
    _sample,
    _tile_bboxes,
    fetch_trails,
    ingest_trails,
    ingest_trails_region,
    resolve_trail_network,
    trailhead_network,
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
    # (id, name, kind, source, url, center_lat, center_lng, geojson)
    assert row[0] == "osm:way/100"
    assert row[1] == "Ridge Trail"
    assert row[2] == "path"
    assert row[3] == "osm"
    assert row[4] == "https://www.openstreetmap.org/way/100"
    assert row[5] == pytest.approx(47.62) and row[6] == pytest.approx(-122.28)  # center = flat[len//2]
    geometry = json.loads(row[7])
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
    assert json.loads(row[7])["type"] == "Point"


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
    geometry = json.loads(row[7])
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


def test_trails_near_limit_returns_only_the_nearest(con: psycopg.Connection) -> None:
    elements = [
        _parse_element(
            {
                "type": "way",
                "id": i,
                "tags": {"highway": "path", "name": f"T{i}"},
                "geometry": [{"lat": 47.6 + i * 0.02, "lon": -122.3}, {"lat": 47.61 + i * 0.02, "lon": -122.29}],
            }
        )
        for i in range(1, 4)
    ]
    upsert_trails(con, [e for e in elements if e is not None])
    trails = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, limit=1)
    assert [trail.name for trail in trails] == ["T1"]


def test_trails_near_skips_camp_distance_when_disabled(con: psycopg.Connection) -> None:
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
    upsert_campsites(con, [("ridb:1", "Camp", "campground", None, True, 47.6, -122.3, "ridb", "http://x")])
    trails = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0, with_camp_distance=False)
    assert trails[0].camp_distance_km is None  # LATERAL skipped even though a camp is in range


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
    cfg = Settings(
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
    from foray.sources import overpass

    assert overpass.bbox(45.5, -124.8, 49.0, -116.9) == "(45.5,-124.8,49.0,-116.9)"


def test_tile_bboxes_covers_a_wide_region_in_bounded_tiles() -> None:
    # Washington: ~7.9 degrees wide, ~3.5 tall - bigger than one 2-degree tile in both axes.
    tiles = _tile_bboxes(45.5438, -124.8485, 49.002, -116.9156)
    assert len(tiles) > 1
    for min_lat, min_lng, max_lat, max_lng in tiles:
        assert max_lat - min_lat <= 2.0 + 1e-9
        assert max_lng - min_lng <= 2.0 + 1e-9
    # Every point in the original bbox is covered by at least one tile.
    assert min(t[0] for t in tiles) == pytest.approx(45.5438)
    assert min(t[1] for t in tiles) == pytest.approx(-124.8485)
    assert max(t[2] for t in tiles) == pytest.approx(49.002)
    assert max(t[3] for t in tiles) == pytest.approx(-116.9156)


def test_tile_bboxes_single_tile_for_a_small_region() -> None:
    assert _tile_bboxes(45.0, -123.0, 46.0, -122.0) == [(45.0, -123.0, 46.0, -122.0)]


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
    # Washington's bbox spans multiple 2-degree tiles, so this same trail comes back from every
    # tile query - `count` is rows upserted (may exceed the number of distinct trails), one per
    # tile, while the `trails` table itself stays deduped by id via ON CONFLICT.
    region = CoverageRegion(name="Washington", place_id=46, bbox=(-124.8, 45.5, -116.9, 49.0))
    expected_tiles = len(_tile_bboxes(45.5, -124.8, 49.0, -116.9))
    count = ingest_trails_region(region, con, client=client)
    assert count == expected_tiles
    assert con.execute("SELECT count(*) FROM trails").fetchone() == (1,)
    assert is_ingested(con, "trails:place:46")
    # Second call skips before ever opening a client - if it didn't, this would try (and fail)
    # to reach the real Overpass API, since no client is passed here.
    assert ingest_trails_region(region, con) == 0


def test_ingest_trails_region_does_not_mark_ingested_when_a_tile_fails(con: psycopg.Connection) -> None:
    ok_response = httpx.Response(
        200,
        json={
            "elements": [
                {
                    "type": "way",
                    "id": 9,
                    "tags": {"highway": "path", "name": "Partial Trail"},
                    "geometry": [
                        {"lat": 45.6, "lon": -124.0},
                        {"lat": 45.7, "lon": -123.9},
                    ],
                }
            ]
        },
    )
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # First tile succeeds, every other tile fails - simulates a transient Overpass outage
        # partway through a region.
        return ok_response if calls["n"] == 1 else httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    region = CoverageRegion(name="Washington", place_id=46, bbox=(-124.8, 45.5, -116.9, 49.0))
    count = ingest_trails_region(region, con, client=client)
    assert count == 1  # only the one tile that succeeded
    assert con.execute("SELECT count(*) FROM trails").fetchone() == (1,)  # its row is still cached
    assert not is_ingested(con, "trails:place:46")  # not marked done - a retry should fill the gaps


def test_trails_near_filters_by_kind_and_caps_with_limit(con: psycopg.Connection) -> None:
    trailhead = _parse_element(
        {"type": "node", "id": 1, "lat": 47.61, "lon": -122.31, "tags": {"highway": "trailhead"}}
    )
    path = _parse_element(
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "path", "name": "Ridge"},
            "geometry": [{"lat": 47.62, "lon": -122.30}, {"lat": 47.63, "lon": -122.29}],
        }
    )
    assert trailhead is not None and path is not None
    upsert_trails(con, [trailhead, path])

    only_trailheads = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0, kind="trailhead")
    assert [trail.kind for trail in only_trailheads] == ["trailhead"]

    capped = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0, limit=1)
    assert len(capped) == 1


def test_get_trail_round_trips_by_id(con: psycopg.Connection) -> None:
    row = _parse_element(
        {"type": "node", "id": 5, "lat": 47.6, "lon": -122.3, "tags": {"highway": "trailhead", "name": "TH"}}
    )
    assert row is not None
    upsert_trails(con, [row])
    trail = get_trail(con, "osm:node/5")
    assert trail is not None
    assert trail.name == "TH"
    assert trail.kind == "trailhead"


def test_get_trail_missing_id_returns_none(con: psycopg.Connection) -> None:
    assert get_trail(con, "osm:node/999") is None


def test_nearest_trail_finds_closest_within_max_km(con: psycopg.Connection) -> None:
    near = _parse_element(
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "path", "name": "Near"},
            "geometry": [{"lat": 47.605, "lon": -122.305}, {"lat": 47.606, "lon": -122.304}],
        }
    )
    far = _parse_element(
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "path", "name": "Far"},
            "geometry": [{"lat": 48.0, "lon": -123.0}, {"lat": 48.01, "lon": -122.99}],
        }
    )
    assert near is not None and far is not None
    upsert_trails(con, [near, far])
    found = nearest_trail(con, lat=HOME_LAT, lng=HOME_LNG, max_km=2.0)
    assert found is not None
    assert found.name == "Near"


def test_nearest_trail_returns_none_outside_max_km(con: psycopg.Connection) -> None:
    far = _parse_element(
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "path", "name": "Far"},
            "geometry": [{"lat": 48.0, "lon": -123.0}, {"lat": 48.01, "lon": -122.99}],
        }
    )
    assert far is not None
    upsert_trails(con, [far])
    assert nearest_trail(con, lat=HOME_LAT, lng=HOME_LNG, max_km=2.0) is None


def test_network_query_filters_on_the_trailhead_node_and_highway_ways() -> None:
    query = _network_query(123)
    assert "node(id:123);" in query
    assert 'way(bn)["highway"]' in query
    assert 'rel(bw.segs)["route"="hiking"]' in query


def test_parse_trailhead_id_extracts_the_numeric_node_id() -> None:
    assert _parse_trailhead_id("osm:node/456") == 456


def test_parse_trailhead_id_rejects_non_trailhead_ids() -> None:
    with pytest.raises(ValueError, match="not a trailhead"):
        _parse_trailhead_id("osm:way/456")


def test_trailhead_network_merges_way_and_route_members() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "way",
                        "id": 10,
                        "tags": {"highway": "path"},
                        "geometry": [{"lat": 47.6, "lon": -122.3}, {"lat": 47.61, "lon": -122.29}],
                    },
                    {
                        "type": "relation",
                        "id": 20,
                        "tags": {"route": "hiking", "name": "Ridge Loop"},
                        "members": [
                            {
                                "type": "way",
                                "geometry": [{"lat": 47.61, "lon": -122.29}, {"lat": 47.62, "lon": -122.28}],
                            }
                        ],
                    },
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = trailhead_network(1, client=client)
    assert result is not None
    assert result["name"] == "Ridge Loop"
    assert result["kind"] == "route"
    assert result["geometry"]["type"] == "MultiLineString"
    assert len(result["geometry"]["coordinates"]) == 2


def test_trailhead_network_returns_none_when_no_elements() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"elements": []})))
    assert trailhead_network(1, client=client) is None


def test_trailhead_network_returns_none_on_a_failing_query() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    assert trailhead_network(1, client=client) is None


def test_resolve_trail_network_raises_for_an_unknown_trailhead(con: psycopg.Connection) -> None:
    with pytest.raises(LookupError):
        resolve_trail_network(con, "osm:node/999", client=httpx.Client())


def test_resolve_trail_network_uses_live_topology_when_available(con: psycopg.Connection) -> None:
    trailhead = _parse_element({"type": "node", "id": 1, "lat": 47.6, "lon": -122.3, "tags": {"highway": "trailhead"}})
    assert trailhead is not None
    upsert_trails(con, [trailhead])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "way",
                        "id": 10,
                        "tags": {"highway": "path", "name": "Real Trail"},
                        "geometry": [{"lat": 47.6, "lon": -122.3}, {"lat": 47.61, "lon": -122.29}],
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = resolve_trail_network(con, "osm:node/1", client=client)
    assert result is not None
    assert result.authoritative is True
    assert result.trail.name == "Real Trail"
    assert result.trail.kind == "path"


def test_resolve_trail_network_falls_back_to_nearest_cached_trail(con: psycopg.Connection) -> None:
    trailhead = _parse_element({"type": "node", "id": 1, "lat": 47.6, "lon": -122.3, "tags": {"highway": "trailhead"}})
    nearby_path = _parse_element(
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "path", "name": "Nearby"},
            "geometry": [{"lat": 47.601, "lon": -122.301}, {"lat": 47.602, "lon": -122.302}],
        }
    )
    assert trailhead is not None and nearby_path is not None
    upsert_trails(con, [trailhead, nearby_path])

    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"elements": []})))
    result = resolve_trail_network(con, "osm:node/1", client=client)
    assert result is not None
    assert result.authoritative is False
    assert result.trail.name == "Nearby"


def test_resolve_trail_network_returns_none_when_nothing_found_at_all(con: psycopg.Connection) -> None:
    trailhead = _parse_element({"type": "node", "id": 1, "lat": 47.6, "lon": -122.3, "tags": {"highway": "trailhead"}})
    assert trailhead is not None
    upsert_trails(con, [trailhead])
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"elements": []})))
    assert resolve_trail_network(con, "osm:node/1", client=client) is None
