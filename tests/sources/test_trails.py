"""Trail ingest + scoring tests - no network (mocked Overpass transport)."""

from __future__ import annotations

import json

import httpx
import psycopg
import pytest

from foray.cache import is_ingested, record_ingest, upsert_campsites, upsert_public_land, upsert_trails
from foray.config import CoverageRegion, Home, Ingest, Settings
from foray.scoring import get_trail, nearest_trail, trail_land_units, trail_segments_by_name, trails_near
from foray.sources.trails import (
    _TRAILS_QUERY_VERSION,
    _network_query,
    _parse_element,
    _parse_trailhead_id,
    _parse_trails,
    _sample,
    _tile_bboxes,
    _trails_query,
    _trails_query_bbox,
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


def test_parse_element_derives_length_km_and_keeps_detail_tags() -> None:
    row = _parse_element(
        {
            "type": "way",
            "id": 1,
            "tags": {
                "highway": "path",
                "name": "Ridge",
                "surface": "dirt",
                "sac_scale": "mountain_hiking",
                "informal": "yes",
                "foot": "yes",
                "wikipedia": "en:Ridge Trail",  # not in _ATTR_TAGS - dropped
            },
            "geometry": [{"lat": 47.60, "lon": -122.30}, {"lat": 47.61, "lon": -122.30}],  # ~1.1 km
        }
    )
    assert row is not None
    assert 1.0 < row[9] < 1.3  # length_km, great-circle over the full polyline
    assert json.loads(row[10]) == {
        "highway": "path",
        "surface": "dirt",
        "sac_scale": "mountain_hiking",
        "informal": "yes",
        "foot": "yes",
    }

    bare = _parse_element(
        {"type": "way", "id": 2, "tags": {"highway": "path"}, "geometry": [{"lat": 47.6, "lon": -122.3}]}
    )
    # single vertex -> no length; `highway` is always kept so attrs is never fully empty now
    assert bare is not None and bare[9] is None and json.loads(bare[10]) == {"highway": "path"}


def test_parse_element_classifies_a_forest_road_and_keeps_road_tags() -> None:
    track = _parse_element(
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "track", "ref": "FR 300", "tracktype": "grade3", "access": "yes"},
            "geometry": [{"lat": 47.60, "lon": -122.30}, {"lat": 47.61, "lon": -122.29}],
        }
    )
    assert track is not None
    assert track[2] == "road"
    assert track[1] == "FR 300"  # ref stands in for a missing name
    assert json.loads(track[10]) == {"highway": "track", "ref": "FR 300", "tracktype": "grade3", "access": "yes"}

    forestry_service = _parse_element(
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "service", "service": "forestry"},
            "geometry": [{"lat": 47.60, "lon": -122.30}, {"lat": 47.61, "lon": -122.29}],
        }
    )
    assert forestry_service is not None
    assert forestry_service[2] == "road"
    assert forestry_service[1] == "Forest road (OSM)"  # unnamed -> road-specific fallback

    # `service=forestry` is the qualifier - a plain service way (never asked for by the query,
    # but defensive) is not treated as a forest road
    plain_service = _parse_element(
        {
            "type": "way",
            "id": 3,
            "tags": {"highway": "service"},
            "geometry": [{"lat": 47.60, "lon": -122.30}, {"lat": 47.61, "lon": -122.29}],
        }
    )
    assert plain_service is not None and plain_service[2] == "path"


def test_parse_element_classifies_a_bridleway_as_a_path() -> None:
    row = _parse_element(
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "bridleway", "name": "Horse Loop"},
            "geometry": [{"lat": 47.60, "lon": -122.30}, {"lat": 47.61, "lon": -122.29}],
        }
    )
    assert row is not None and row[2] == "path"


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


def test_parse_trails_links_a_trailhead_to_the_path_it_sits_on() -> None:
    payload = {
        "elements": [
            # trailhead node right on the first vertex of way 2
            {"type": "node", "id": 1, "lat": 47.600, "lon": -122.300, "tags": {"highway": "trailhead"}},
            {
                "type": "way",
                "id": 2,
                "tags": {"highway": "path", "name": "On It"},
                "geometry": [{"lat": 47.600, "lon": -122.300}, {"lat": 47.602, "lon": -122.298}],
            },
            {  # a way ~400 m away - outside _LINK_SNAP_M, must not link
                "type": "way",
                "id": 3,
                "tags": {"highway": "path", "name": "Far Off"},
                "geometry": [{"lat": 47.604, "lon": -122.300}, {"lat": 47.605, "lon": -122.299}],
            },
        ]
    }
    rows = {row[0]: row for row in _parse_trails(payload)}
    assert rows["osm:node/1"][8] == ["osm:way/2"]  # connects: only the path it touches
    assert rows["osm:way/2"][8] is None  # non-trailhead rows carry no connects


def test_parse_trails_links_a_trailhead_to_the_whole_named_trail() -> None:
    # OSM splits "Ridge Trail" into ways 2 and 3; the node touches only way 2, but the link
    # covers both so selecting it draws the whole trail (issue #306).
    payload = {
        "elements": [
            {"type": "node", "id": 1, "lat": 47.600, "lon": -122.300, "tags": {"highway": "trailhead"}},
            {
                "type": "way",
                "id": 2,
                "tags": {"highway": "path", "name": "Ridge Trail"},
                "geometry": [{"lat": 47.600, "lon": -122.300}, {"lat": 47.602, "lon": -122.298}],
            },
            {
                "type": "way",
                "id": 3,
                "tags": {"highway": "path", "name": "Ridge Trail"},  # far segment, same name
                "geometry": [{"lat": 47.640, "lon": -122.260}, {"lat": 47.650, "lon": -122.250}],
            },
            {
                "type": "way",
                "id": 4,
                "tags": {"highway": "path", "name": "Other Trail"},  # different name, not linked
                "geometry": [{"lat": 47.601, "lon": -122.300}, {"lat": 47.603, "lon": -122.298}],
            },
        ]
    }
    rows = {row[0]: row for row in _parse_trails(payload)}
    assert rows["osm:node/1"][8] == ["osm:way/2", "osm:way/3"]


def test_parse_trails_links_a_trailhead_to_a_whole_unnamed_forest_road_by_ref() -> None:
    # OSM splits "FR 300" into unnamed `highway=track` segments that share only `ref` + `operator`.
    # The node touches segment 2; the link should still cover the far segment 3 (same ref+operator)
    # but not segment 4 (different ref).
    payload = {
        "elements": [
            {"type": "node", "id": 1, "lat": 47.600, "lon": -122.300, "tags": {"highway": "trailhead"}},
            {
                "type": "way",
                "id": 2,
                "tags": {"highway": "track", "ref": "FR 300", "operator": "US Forest Service"},
                "geometry": [{"lat": 47.600, "lon": -122.300}, {"lat": 47.602, "lon": -122.298}],
            },
            {
                "type": "way",
                "id": 3,
                "tags": {"highway": "track", "ref": "FR 300", "operator": "US Forest Service"},
                "geometry": [{"lat": 47.640, "lon": -122.260}, {"lat": 47.650, "lon": -122.250}],
            },
            {
                "type": "way",
                "id": 4,
                "tags": {"highway": "track", "ref": "FR 12", "operator": "US Forest Service"},
                "geometry": [{"lat": 47.601, "lon": -122.300}, {"lat": 47.603, "lon": -122.298}],
            },
        ]
    }
    rows = {row[0]: row for row in _parse_trails(payload)}
    assert rows["osm:node/1"][8] == ["osm:way/2", "osm:way/3"]
    assert rows["osm:way/2"][2] == "road"


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
        [("ridb:1", "Camp", "campground", None, True, 47.6, -122.3, "ridb", "http://x", None, None, None)],
    )
    trails = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0)
    assert trails[0].camp_distance_km is not None
    assert trails[0].camp_distance_km < 5.0  # the campsite sits right on the trail's center


def test_trails_near_limit_returns_only_the_nearest(con: psycopg.Connection) -> None:
    elements = []
    for i in range(1, 4):
        base = 47.6 + i * 0.02  # each trail a bit farther north than the last
        element = _parse_element(
            {
                "type": "way",
                "id": i,
                "tags": {"highway": "path", "name": f"T{i}"},
                "geometry": [{"lat": base, "lon": -122.3}, {"lat": base + 0.01, "lon": -122.29}],
            }
        )
        assert element is not None
        elements.append(element)
    upsert_trails(con, elements)
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
    upsert_campsites(
        con,
        [("ridb:1", "Camp", "campground", None, True, 47.6, -122.3, "ridb", "http://x", None, None, None)],
    )
    trails = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0, with_camp_distance=False)
    assert trails[0].camp_distance_km is None  # LATERAL skipped even though a camp is in range


def test_trails_near_omits_geometry_when_disabled(con: psycopg.Connection) -> None:
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
    with_geom = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0)
    assert with_geom[0].geometry is not None and with_geom[0].geometry["type"] == "LineString"
    without_geom = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=30.0, with_geometry=False)
    assert without_geom[0].geometry is None
    assert without_geom[0].name == "Ridge"  # the rest of the row is intact


def test_trails_near_no_rows_ingested_returns_empty(con: psycopg.Connection) -> None:
    assert trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0) == []


def _seed_trailhead_network(con: psycopg.Connection) -> None:
    """A route + long path + short unnamed stub, and three trailheads that lead to them."""
    route = _parse_element(
        {
            "type": "relation",
            "id": 1,
            "tags": {"route": "hiking", "name": "Ridge Route"},
            "members": [
                {"type": "way", "geometry": [{"lat": 47.700, "lon": -122.30}, {"lat": 47.740, "lon": -122.30}]},
            ],
        }
    )
    long_path = _parse_element(
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "path", "name": "Long Path"},
            "geometry": [{"lat": 47.600, "lon": -122.30}, {"lat": 47.620, "lon": -122.30}],  # ~2.2 km
        }
    )
    stub = _parse_element(
        {
            "type": "way",
            "id": 3,
            "tags": {"highway": "path"},  # unnamed
            "geometry": [{"lat": 47.6100, "lon": -122.310}, {"lat": 47.6108, "lon": -122.310}],  # ~90 m
        }
    )
    assert route and long_path and stub

    def th(node: int, lat: float, lng: float, name: str, connects: list[str]) -> tuple[object, ...]:
        point = f'{{"type":"Point","coordinates":[{lng},{lat}]}}'
        return (f"osm:node/{node}", name, "trailhead", "osm", "u", lat, lng, point, connects, None, None)

    upsert_trails(
        con,
        [
            route,
            long_path,
            stub,
            th(10, 47.605, -122.30, "Long Path TH", ["osm:way/2"]),  # ~0.6 km from home
            th(11, 47.700, -122.30, "Route TH", ["osm:relation/1"]),  # ~10 km - farther but a route
            th(12, 47.610, -122.310, "Trailhead (OSM)", ["osm:way/3"]),  # unnamed, only a stub
        ],
    )


def test_trails_near_relevance_ranks_the_route_trailhead_over_a_closer_spur(con: psycopg.Connection) -> None:
    _seed_trailhead_network(con)
    by_relevance = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, kind="trailhead", sort="relevance")
    assert [t.name for t in by_relevance[:2]] == ["Route TH", "Long Path TH"]
    # nearest still puts the closest trailhead first
    by_distance = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, kind="trailhead")
    assert by_distance[0].name == "Long Path TH"


def test_trails_near_significant_only_drops_the_stub_trailhead(con: psycopg.Connection) -> None:
    _seed_trailhead_network(con)
    kept = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, kind="trailhead", significant_only=True)
    assert "Trailhead (OSM)" not in {t.name for t in kept}
    assert {"Route TH", "Long Path TH"} <= {t.name for t in kept}


def test_trails_near_obs_density_lifts_a_trail_through_a_hotspot(con: psycopg.Connection) -> None:
    # Two named paths in range: "Popular" a bit farther, "Quiet" closer. Seed target-genus
    # research-grade observations right on "Popular"; with a genus filter the relevance sort
    # puts it first, without one it falls back to distance/length.
    paths = [
        _parse_element(
            {
                "type": "way",
                "id": 1,
                "tags": {"highway": "path", "name": "Popular"},
                "geometry": [{"lat": 47.610, "lon": -122.30}, {"lat": 47.615, "lon": -122.30}],
            }
        ),
        _parse_element(
            {
                "type": "way",
                "id": 2,
                "tags": {"highway": "path", "name": "Quiet"},
                "geometry": [{"lat": 47.601, "lon": -122.30}, {"lat": 47.606, "lon": -122.30}],
            }
        ),
    ]
    assert all(path is not None for path in paths)
    upsert_trails(con, [path for path in paths if path is not None])
    with con.cursor() as cur:
        cur.execute("INSERT INTO fungi_genera (taxon_id, name) VALUES (48701, 'Boletus')")
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, quality_grade)"
            " VALUES (%s, 48701, %s, %s, '2022-09-15', 9, 'research')",
            [(i, 47.612, -122.30) for i in range(1, 9)],  # 8 finds on "Popular"
        )

    with_genus = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, sort="relevance", taxon_ids=[48701])
    assert [t.name for t in with_genus[:2]] == ["Popular", "Quiet"]
    no_genus = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, sort="relevance")
    assert [t.name for t in no_genus[:2]] == ["Quiet", "Popular"]  # equal prominence -> nearer first


def test_trails_near_road_relevance_ranks_obs_density_over_length(con: psycopg.Connection) -> None:
    # A long forest road with no finds vs a short one running through a hotspot: for kind='road'
    # the relevance sort is obs-first, so the short road with finds wins (a plain path sort would
    # rank the long one higher on length).
    long_empty = _parse_element(
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "track", "name": "Long Road"},
            "geometry": [{"lat": 47.60, "lon": -122.30}, {"lat": 47.75, "lon": -122.30}],
        }
    )
    short_hot = _parse_element(
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "track", "name": "Hot Road"},
            "geometry": [{"lat": 47.601, "lon": -122.31}, {"lat": 47.606, "lon": -122.31}],
        }
    )
    assert long_empty is not None and short_hot is not None
    upsert_trails(con, [long_empty, short_hot])
    with con.cursor() as cur:
        cur.execute("INSERT INTO fungi_genera (taxon_id, name) VALUES (48701, 'Boletus')")
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, quality_grade)"
            " VALUES (%s, 48701, %s, %s, '2022-09-15', 9, 'research')",
            [(i, 47.603, -122.31) for i in range(1, 7)],  # 6 finds on "Hot Road"
        )
    ranked = trails_near(
        con, lat=HOME_LAT, lng=HOME_LNG, radius_km=80.0, kind="road", sort="relevance", taxon_ids=[48701]
    )
    assert [t.name for t in ranked] == ["Hot Road", "Long Road"]


def test_trails_near_flags_a_walk_in_road(con: psycopg.Connection) -> None:
    gated = _parse_element(
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "track", "name": "Gated Road", "motor_vehicle": "no", "foot": "yes"},
            "geometry": [{"lat": 47.601, "lon": -122.30}, {"lat": 47.606, "lon": -122.30}],
        }
    )
    drivable = _parse_element(
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "track", "name": "Open Road"},
            "geometry": [{"lat": 47.601, "lon": -122.32}, {"lat": 47.606, "lon": -122.32}],
        }
    )
    # `access=no` closes every mode (incl. foot) by OSM convention - not walk-in without an
    # explicit `foot` re-grant.
    fully_closed = _parse_element(
        {
            "type": "way",
            "id": 3,
            "tags": {"highway": "track", "name": "Closed Road", "access": "no"},
            "geometry": [{"lat": 47.601, "lon": -122.34}, {"lat": 47.606, "lon": -122.34}],
        }
    )
    closed_but_walkable = _parse_element(
        {
            "type": "way",
            "id": 4,
            "tags": {"highway": "track", "name": "Foot OK Road", "access": "private", "foot": "permissive"},
            "geometry": [{"lat": 47.601, "lon": -122.36}, {"lat": 47.606, "lon": -122.36}],
        }
    )
    roads = [gated, drivable, fully_closed, closed_but_walkable]
    assert all(road is not None for road in roads)
    upsert_trails(con, [road for road in roads if road is not None])
    by_name = {t.name: t for t in trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, kind="road")}
    assert by_name["Gated Road"].walk_in is True
    assert by_name["Open Road"].walk_in is False
    assert by_name["Closed Road"].walk_in is False
    assert by_name["Foot OK Road"].walk_in is True


_GATE_PAYLOAD = {
    "elements": [
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "track", "name": "FR 300"},
            "geometry": [{"lat": 47.6000, "lon": -122.30}, {"lat": 47.6050, "lon": -122.30}],
        },
        {  # a gate sitting on FR 300's first vertex
            "type": "node",
            "id": 50,
            "lat": 47.6000,
            "lon": -122.30,
            "tags": {"barrier": "gate"},
        },
        {  # a lone gate nowhere near a road
            "type": "node",
            "id": 51,
            "lat": 48.0,
            "lon": -121.0,
            "tags": {"barrier": "gate"},
        },
    ]
}


def test_parse_trails_marks_a_road_with_a_gate_node_as_walk_in() -> None:
    rows = {row[0]: row for row in _parse_trails(_GATE_PAYLOAD)}
    assert set(rows) == {"osm:way/1"}  # the two barrier nodes never become trailhead rows
    attrs = json.loads(rows["osm:way/1"][10])
    assert attrs["barrier"] == "gate"


def test_trails_near_flags_a_gated_road_walk_in(con: psycopg.Connection) -> None:
    upsert_trails(con, _parse_trails(_GATE_PAYLOAD))
    (road,) = trails_near(con, lat=47.60, lng=-122.30, radius_km=20.0, kind="road")
    assert road.name == "FR 300"
    assert road.walk_in is True


def test_walk_in_gate_is_overridden_by_an_explicit_foot_no() -> None:
    from foray.scoring.queries import _walk_in

    assert _walk_in({"barrier": "gate"}) is True
    assert _walk_in({"barrier": "gate", "foot": "no"}) is False
    assert _walk_in({"barrier": "cattle_grid"}) is False  # vehicles drive over it
    # an explicit legal closure still wins over the physical gate
    assert _walk_in({"barrier": "gate", "access": "private"}) is False
    assert _walk_in({"barrier": "gate", "access": "private", "foot": "yes"}) is True


def test_trail_land_units_and_get_trail_tag_the_smallest_owning_unit(con: psycopg.Connection) -> None:
    road = _parse_element(
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "track", "name": "FR 12"},
            "geometry": [{"lat": 47.60, "lon": -122.30}, {"lat": 47.61, "lon": -122.30}],
        }
    )
    assert road is not None
    upsert_trails(con, [road])
    # A big forest polygon and a small wilderness polygon, both covering the road - smallest wins.
    forest = '{"type":"Polygon","coordinates":[[[-123,47],[-121,47],[-121,48],[-123,48],[-123,47]]]}'
    wild = '{"type":"Polygon","coordinates":[[[-122.4,47.5],[-122.2,47.5],[-122.2,47.7],[-122.4,47.7],[-122.4,47.5]]]}'
    upsert_public_land(
        con,
        [
            ("pl:1", "USFS", "Big National Forest", "usfs", "u", forest),
            ("pl:2", "USFS", "Small Wilderness", "usfs", "u", wild),
        ],
    )
    assert trail_land_units(con, ["osm:way/1"]) == {"osm:way/1": ("USFS", "Small Wilderness")}
    assert trail_land_units(con, []) == {}
    assert trail_land_units(con, ["osm:way/999"]) == {}  # unknown id -> absent, not a null entry
    single = get_trail(con, "osm:way/1")
    assert single is not None and (single.land_agency, single.land_unit) == ("USFS", "Small Wilderness")


def test_trails_near_dedupes_same_named_trailheads(con: psycopg.Connection) -> None:
    def th(node: int, lat: float) -> tuple[object, ...]:
        point = f'{{"type":"Point","coordinates":[-122.30,{lat}]}}'
        return (f"osm:node/{node}", "Beaver Pond", "trailhead", "osm", "u", lat, -122.30, point, None, None, None)

    upsert_trails(con, [th(1, 47.61), th(2, 47.62), th(3, 47.63)])
    result = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, kind="trailhead")
    assert [t.name for t in result] == ["Beaver Pond"]  # three nodes, one row


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
    assert is_ingested(con, f"trails:place:46:q{_TRAILS_QUERY_VERSION}")
    # Second call skips before ever opening a client - if it didn't, this would try (and fail)
    # to reach the real Overpass API, since no client is passed here.
    assert ingest_trails_region(region, con) == 0
    # ...unless forced: re-fetches without a query-version bump (OSM drift / debugging).
    assert ingest_trails_region(region, con, client=client, force=True) == expected_tiles


def test_ingest_trails_region_re_pulls_a_region_ingested_under_an_older_query_version(
    con: psycopg.Connection,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "way",
                        "id": 7,
                        "tags": {"highway": "track", "ref": "FR 10"},
                        "geometry": [{"lat": 47.61, "lon": -122.31}, {"lat": 47.62, "lon": -122.30}],
                    }
                ]
            },
        )

    region = CoverageRegion(name="Washington", place_id=46, bbox=(-124.8, 45.5, -116.9, 49.0))
    # Simulate a prior ingest under an older query: the pre-versioning key and a stale q1 marker.
    record_ingest(con, "trails:place:46", 5)
    record_ingest(con, "trails:place:46:q1", 5)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    count = ingest_trails_region(region, con, client=client)
    assert count > 0  # the stale markers didn't match the current key, so it re-pulled
    assert is_ingested(con, f"trails:place:46:q{_TRAILS_QUERY_VERSION}")
    assert not is_ingested(con, "trails:place:46")  # superseded markers pruned on success
    assert not is_ingested(con, "trails:place:46:q1")


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
    assert not is_ingested(con, f"trails:place:46:q{_TRAILS_QUERY_VERSION}")  # not done - a retry fills the gaps


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


def test_nearest_trail_includes_forest_roads(con: psycopg.Connection) -> None:
    road = _parse_element(
        {
            "type": "way",
            "id": 1,
            "tags": {"highway": "track", "ref": "FR 12"},
            "geometry": [{"lat": 47.601, "lon": -122.301}, {"lat": 47.602, "lon": -122.302}],
        }
    )
    assert road is not None and road[2] == "road"
    upsert_trails(con, [road])
    found = nearest_trail(con, lat=HOME_LAT, lng=HOME_LNG, max_km=2.0)
    assert found is not None and found.name == "FR 12" and found.kind == "road"


def test_network_query_filters_on_the_trailhead_node_and_highway_ways() -> None:
    query = _network_query(123)
    assert "node(id:123);" in query
    assert 'way(bn)["highway"]' in query
    assert 'rel(bw.segs)["route"="hiking"]' in query


def test_trails_query_fetches_relations_in_a_separate_out_statement() -> None:
    # Inside a union, `out geom` returns a route relation with only `bounds` and no members, so
    # `_parse_element` can never build a `kind='route'` row (issue #306). Both the radius and the
    # bbox query must emit the relation clause after the way/node union closes, with its own out.
    for query in (
        _trails_query(HOME_LAT, HOME_LNG, 5000),
        _trails_query_bbox(47.0, -123.0, 48.0, -122.0),
    ):
        # relation clause sits after the way/node union closes (`);`), with its own `out geom;`
        assert query.index('relation["route"="hiking"]') > query.index(");")
        _, _, after_relation = query.partition('relation["route"="hiking"]')
        assert after_relation.rstrip().endswith("out geom;")


def test_trails_query_fetches_trails_forest_roads_and_trailheads() -> None:
    for query in (
        _trails_query(HOME_LAT, HOME_LNG, 5000),
        _trails_query_bbox(47.0, -123.0, 48.0, -122.0),
    ):
        assert 'way["highway"~"^(path|bridleway)$"]' in query
        assert 'way["highway"~"^(track)$"]' in query
        assert 'way["highway"="service"]["service"="forestry"]' in query
        assert 'node["highway"="trailhead"]' in query
        assert '"footway"' not in query  # deliberately excluded (sidewalk noise)


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


def test_resolve_trail_network_uses_the_cached_link_without_a_live_call(con: psycopg.Connection) -> None:
    path = _parse_element(
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "path", "name": "Cached Ridge"},
            "geometry": [{"lat": 47.60, "lon": -122.30}, {"lat": 47.61, "lon": -122.29}],
        }
    )
    assert path is not None
    trailhead = (
        "osm:node/1",
        "TH",
        "trailhead",
        "osm",
        "u",
        47.60,
        -122.30,
        '{"type":"Point","coordinates":[-122.30,47.60]}',
        ["osm:way/2"],
        None,  # length_km
        None,  # attrs
    )
    upsert_trails(con, [trailhead, path])

    def boom(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("resolve_trail_network made a live Overpass call despite a cached link")

    result = resolve_trail_network(con, "osm:node/1", client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert result is not None
    assert result.authoritative is True
    assert result.trail.name == "Cached Ridge"
    assert result.trail.geometry is not None and result.trail.geometry["type"] == "LineString"


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

    # The live lookup was written back: the way is now cached and the trailhead links to it, so
    # a second call needs no network (issue #306).
    stored = con.execute("SELECT connects FROM trails WHERE id = 'osm:node/1'").fetchone()
    assert stored is not None and stored[0] == ["osm:way/10"]
    boom = httpx.Client(transport=httpx.MockTransport(lambda _r: (_ for _ in ()).throw(AssertionError("live call"))))
    again = resolve_trail_network(con, "osm:node/1", client=boom)
    assert again is not None and again.trail.name == "Real Trail"


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


def _named_way(way_id: int, name: str, coords: list[tuple[float, float]]) -> tuple[object, ...]:
    row = _parse_element(
        {
            "type": "way",
            "id": way_id,
            "tags": {"highway": "path", "name": name},
            "geometry": [{"lat": lat, "lon": lng} for lat, lng in coords],
        }
    )
    assert row is not None
    return row


def test_trails_near_multi_kind_lists_trailheads_and_named_paths(con: psycopg.Connection) -> None:
    # A park with one trailhead node but several marquee named paths - the card asks for all
    # three kinds so the paths aren't dropped (issue #306 C1).
    trailhead = _parse_element(
        {"type": "node", "id": 1, "lat": 47.605, "lon": -122.30, "tags": {"highway": "trailhead", "name": "Fern TH"}}
    )
    assert trailhead is not None
    upsert_trails(
        con,
        [
            trailhead,
            _named_way(2, "James Irvine Trail", [(47.606, -122.30), (47.612, -122.30)]),
            _named_way(3, "Miner's Ridge Trail", [(47.607, -122.31), (47.613, -122.31)]),
        ],
    )
    names = {t.name for t in trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, kind="trailhead,path,route")}
    assert names == {"Fern TH", "James Irvine Trail", "Miner's Ridge Trail"}
    # A single kind still works (the `= ANY` collapse of the old `= %s`).
    only_th = trails_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, kind="trailhead")
    assert [t.name for t in only_th] == ["Fern TH"]


def test_trails_near_significant_only_collapses_same_named_path_segments(con: psycopg.Connection) -> None:
    upsert_trails(
        con,
        [
            _named_way(2, "James Irvine Trail", [(47.601, -122.30), (47.606, -122.30)]),
            _named_way(3, "James Irvine Trail", [(47.606, -122.30), (47.611, -122.30)]),
            _named_way(4, "James Irvine Trail", [(47.611, -122.30), (47.616, -122.30)]),
        ],
    )
    listed = trails_near(
        con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, kind="path", significant_only=True, sort="relevance"
    )
    assert [t.name for t in listed] == ["James Irvine Trail"]


def test_resolve_trail_network_draws_a_selected_path_stitched_by_name(con: psycopg.Connection) -> None:
    upsert_trails(
        con,
        [
            _named_way(2, "James Irvine Trail", [(47.601, -122.30), (47.606, -122.30)]),
            _named_way(3, "James Irvine Trail", [(47.606, -122.30), (47.611, -122.30)]),
            # Same name, but ~200 km away - must not be stitched in.
            _named_way(4, "James Irvine Trail", [(49.401, -122.30), (49.406, -122.30)]),
        ],
    )
    result = resolve_trail_network(con, "osm:way/2", client=httpx.Client())
    assert result is not None
    assert result.authoritative is True
    assert result.trail.name == "James Irvine Trail"
    geometry = result.trail.geometry
    assert geometry is not None
    assert geometry["type"] == "MultiLineString"
    assert len(geometry["coordinates"]) == 2  # the two nearby segments, not the far one


def test_trail_segments_by_name_bounds_by_distance(con: psycopg.Connection) -> None:
    upsert_trails(
        con,
        [
            _named_way(2, "Shared Name", [(47.601, -122.30), (47.606, -122.30)]),
            _named_way(3, "Shared Name", [(49.401, -122.30), (49.406, -122.30)]),
        ],
    )
    near = trail_segments_by_name(con, name="Shared Name", kind="path", ref_id="osm:way/2")
    assert [t.id for t in near] == ["osm:way/2"]
