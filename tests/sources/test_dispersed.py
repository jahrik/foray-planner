"""Dispersed-camping ingest tests - no network (mocked Overpass transport)."""

from __future__ import annotations

import httpx
import psycopg
import pytest

from foray.cache import is_ingested
from foray.config import CoverageRegion, Home, Ingest, Settings
from foray.scoring import camps_near
from foray.sources.dispersed import (
    _DISPERSED_COVERAGE_VERSION,
    _parse_reported,
    _reported_query_bbox,
    fetch_reported_campsites,
    ingest_dispersed,
    ingest_dispersed_coverage,
)

HOME_LAT, HOME_LNG = 47.6, -122.3


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


def test_fetch_reported_campsites_handles_overpass_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("foray.sources.overpass.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reported = fetch_reported_campsites(lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0, client=client)
    assert reported == []


def test_fetch_reported_campsites_retries_on_overpass_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("foray.sources.overpass.time.sleep", lambda _seconds: None)
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(
            200,
            json={"elements": [{"type": "node", "id": 7, "lat": 47.6, "lon": -122.3, "tags": {}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reported = fetch_reported_campsites(lat=HOME_LAT, lng=HOME_LNG, radius_km=5.0, client=client)
    assert calls["count"] >= 2
    assert [row[0] for row in reported] == ["osm:node/7"]


def test_ingest_dispersed_upserts_reported_sites(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("foray.sources.overpass.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
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

    cfg = Settings(
        home=Home(name="Home", lat=HOME_LAT, lng=HOME_LNG, radius_km=40.0),
        cell_deg=0.5,
        ingest=Ingest(since_year=2015, quality_grade="research", recent_weeks=4),
    )

    count = ingest_dispersed(cfg, con, client=client)
    assert count == 1
    sites = camps_near(con, lat=HOME_LAT, lng=HOME_LNG, radius_km=50.0)
    assert [site.name for site in sites] == ["Riverside"]
    assert sites[0].kind == "reported"
    assert sites[0].free is True


def test_reported_query_bbox_uses_a_south_west_north_east_filter() -> None:
    query = _reported_query_bbox(41.0, -124.0, 42.0, -123.0)
    assert "(41.0,-124.0,42.0,-123.0)" in query
    assert '["tourism"="camp_site"]' in query and '["backcountry"="yes"]' in query


def test_ingest_dispersed_coverage_tiles_the_envelope_and_is_one_shot(
    con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("foray.sources.overpass.time.sleep", lambda _seconds: None)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode())
        return httpx.Response(
            200,
            json={
                "elements": [
                    {"type": "node", "id": 1, "lat": 41.3, "lon": -124.0, "tags": {"tourism": "camp_site"}},
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cfg = Settings(
        coverage=[CoverageRegion(name="Redwoods", place_id=5, bbox=(-124.5, 41.0, -123.5, 42.0))],
        cell_deg=0.5,
        ingest=Ingest(since_year=2015, quality_grade="research", recent_weeks=4),
    )

    total = ingest_dispersed_coverage(cfg, con, client=client)
    assert total >= 1
    assert len(calls) >= 1  # tiled the 1x1 deg envelope
    assert is_ingested(con, f"dispersed:coverage:v{_DISPERSED_COVERAGE_VERSION}")
    sites = camps_near(con, lat=41.3, lng=-124.0, radius_km=50.0)
    assert [s.kind for s in sites] == ["reported"]

    # Second run is a no-op (marker already present).
    calls.clear()
    assert ingest_dispersed_coverage(cfg, con, client=client) == 0
    assert calls == []
