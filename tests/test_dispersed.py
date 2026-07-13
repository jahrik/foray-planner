"""Dispersed-camping ingest + proxy tests - no network beyond the local/CI test Postgres
(mocked Overpass transport). The point-in-polygon proxy uses PostGIS, enabled once by
``cache.SCHEMA`` on every connection, so it's always available wherever the test Postgres is.
"""

from __future__ import annotations

import httpx
import psycopg
import pytest
from tests.test_land import BLM  # a LandSource fixture with a small polygon helper

from foray.cache import upsert_public_land
from foray.config import Config, Home
from foray.dispersed import (
    Road,
    _parse_reported,
    _parse_tracks,
    _sample,
    dispersed_proxy_rows,
    fetch_dispersed_sources,
    ingest_dispersed,
)
from foray.land import _parse_feature
from foray.scoring import camps_near

HOME_LAT, HOME_LNG = 47.6, -122.3


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


def test_parse_reported_reads_nodes_ways_and_fee_signal() -> None:
    payload = {
        "elements": [
            {  # a node with an explicit no-fee tag → free asserted
                "type": "node",
                "id": 1,
                "lat": 47.61,
                "lon": -122.31,
                "tags": {"tourism": "camp_site", "name": "Free Flats", "fee": "no"},
            },
            {  # a way carries coords under `center`; a fee tag is noted but not asserted free
                "type": "way",
                "id": 2,
                "center": {"lat": 47.62, "lon": -122.32},
                "tags": {"tourism": "camp_site", "fee": "yes"},
            },
            {  # backcountry node with no name → descriptive fallback, unknown cost
                "type": "node",
                "id": 3,
                "lat": 47.63,
                "lon": -122.33,
                "tags": {"backcountry": "yes"},
            },
        ]
    }
    rows = _parse_reported(payload)
    by_id = {row[0]: row for row in rows}

    assert by_id["osm:node/1"][1] == "Free Flats"
    assert by_id["osm:node/1"][2] == "reported"
    assert by_id["osm:node/1"][4] is True  # free
    assert by_id["osm:node/1"][8] == "https://www.openstreetmap.org/node/1"

    assert by_id["osm:way/2"][3] == "fee required"  # fee tag surfaced
    assert by_id["osm:way/2"][4] is None  # but not asserted free

    assert by_id["osm:node/3"][1] == "Backcountry campsite (OSM)"
    assert by_id["osm:node/3"][4] is None  # unknown cost


def test_parse_reported_skips_elements_without_coords() -> None:
    payload = {"elements": [{"type": "way", "id": 9, "tags": {"tourism": "camp_site"}}]}
    assert _parse_reported(payload) == []


def test_parse_tracks_keeps_named_geometry() -> None:
    payload = {
        "elements": [
            {
                "type": "way",
                "id": 100,
                "tags": {"highway": "track", "ref": "FR 2000"},
                "geometry": [{"lat": 47.6, "lon": -122.3}, {"lat": 47.61, "lon": -122.29}],
            },
            {"type": "node", "id": 5, "lat": 47.6, "lon": -122.3},  # not a way → ignored
            {"type": "way", "id": 101, "tags": {"highway": "track"}},  # no geometry → dropped
        ]
    }
    roads = _parse_tracks(payload)
    assert len(roads) == 1
    assert roads[0].way_id == 100
    assert roads[0].name == "FR 2000"  # falls back to `ref` when `name` is absent
    assert len(roads[0].coords) == 2


def test_sample_thins_to_cap_keeping_endpoints() -> None:
    coords = [(float(index), 0.0) for index in range(100)]
    thinned = _sample(coords, 25)
    assert len(thinned) == 25
    assert thinned[0] == coords[0] and thinned[-1] == coords[-1]
    assert _sample(coords[:10], 25) == coords[:10]  # already under the cap → unchanged


def test_fetch_dispersed_sources_routes_queries_and_skips_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("foray.dispersed.time.sleep", lambda _seconds: None)
    camp_node = {
        "type": "node",
        "id": 1,
        "lat": 47.61,
        "lon": -122.31,
        "tags": {"tourism": "camp_site"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "highway" in body:  # the tracks query is down → must be skipped, not fatal
            return httpx.Response(500)
        return httpx.Response(200, json={"elements": [camp_node]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reported, roads = fetch_dispersed_sources(
        lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, client=client, min_interval=0.0
    )
    assert [row[0] for row in reported] == ["osm:node/1"]
    assert roads == []  # tracks query 500'd → skipped, reported still returned


def test_fetch_dispersed_sources_retries_on_overpass_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("foray.dispersed.time.sleep", lambda _seconds: None)
    calls = {"reported": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "highway" in body:
            return httpx.Response(200, json={"elements": []})
        calls["reported"] += 1
        if calls["reported"] == 1:  # first reported request throttled, then succeeds
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(
            200,
            json={"elements": [{"type": "node", "id": 7, "lat": 47.6, "lon": -122.3, "tags": {}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reported, _roads = fetch_dispersed_sources(
        lat=HOME_LAT, lng=HOME_LNG, radius_km=5.0, client=client, min_interval=0.0
    )
    assert calls["reported"] >= 2  # the 429 was retried, not raised
    assert [row[0] for row in reported] == ["osm:node/7"]


def test_dispersed_proxy_rows_empty_without_roads_or_land(
    con: psycopg.Connection,
) -> None:
    assert dispersed_proxy_rows(con, []) == []  # no roads
    # A road but no cached public land → nothing to intersect, and spatial is never touched.
    road = Road(way_id=1, name=None, coords=((47.6, -122.3),))
    assert dispersed_proxy_rows(con, [road]) == []


def test_dispersed_proxy_rows_keeps_only_tracks_on_public_land(
    con: psycopg.Connection,
) -> None:
    land = _parse_feature(BLM, {"properties": {"OBJECTID": 1}, "geometry": _polygon(47.6, -122.3)})
    assert land is not None
    upsert_public_land(con, [land])  # polygon spans ~47.5..47.7 / -122.4..-122.2

    on_land = Road(way_id=10, name="FR 100", coords=((47.61, -122.31), (47.62, -122.30)))
    off_land = Road(way_id=11, name=None, coords=((40.0, -120.0), (40.1, -120.1)))
    rows = dispersed_proxy_rows(con, [on_land, off_land])

    assert [row[0] for row in rows] == ["osm:way/10"]  # only the track inside the polygon
    proxy = rows[0]
    assert proxy[1] == "FR 100"
    assert proxy[2] == "dispersed"
    assert proxy[4] is True  # free of charge on public land
    assert proxy[8] == "https://www.openstreetmap.org/way/10"


def test_ingest_dispersed_upserts_reported_sites(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("foray.dispersed.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "highway" in body:
            return httpx.Response(200, json={"elements": []})  # no tracks
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "node",
                        "id": 1,
                        "lat": 47.61,
                        "lon": -122.31,
                        "tags": {"tourism": "camp_site", "name": "Riverside", "fee": "no"},
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    cfg = Config(
        home=Home(name="Home", lat=HOME_LAT, lng=HOME_LNG, radius_km=40.0),
        cell_deg=0.5,
        since_year=2015,
        quality_grade="research",
        recent_weeks=4,
    )

    # No public_land cached → the proxy is skipped, reported sites still land in `campsites`.
    count = ingest_dispersed(cfg, con, client=client)
    assert count == 1
    sites = camps_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0)
    assert [site.name for site in sites] == ["Riverside"]
    assert sites[0].kind == "reported"
    assert sites[0].free is True
