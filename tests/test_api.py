"""FastAPI route tests over the shared test Postgres (no network beyond it, per python skill)."""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Iterator

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from foray.api import create_app
from foray.cache import upsert_campsites, upsert_fungi_genera, upsert_trails
from foray.config import Home, Settings
from foray.scoring import TripPlan, build_phenology
from foray.trails import _parse_element

CELL = 0.5
MOREL = 111
CHANT = 222
BOLET = 333
HOME_LAT, HOME_LNG = 47.6, -122.3


@pytest.fixture
def cfg(con: psycopg.Connection) -> Settings:
    upsert_fungi_genera(
        con,
        [
            {"taxon_id": MOREL, "name": "Morchella", "common_name": "Morels"},
            {"taxon_id": CHANT, "name": "Cantharellus", "common_name": "Chanterelles"},
            {"taxon_id": BOLET, "name": "Boletus", "common_name": "King Boletes"},
        ],
    )
    rows = (
        [(obs_id, MOREL, HOME_LAT, HOME_LNG, dt.date(2022, 4, 15), 4, "research", 10) for obs_id in range(1, 11)]
        + [(obs_id, CHANT, HOME_LAT, HOME_LNG, dt.date(2022, 7, 10), 7, "research", 10) for obs_id in range(11, 16)]
        + [(obs_id, BOLET, HOME_LAT, HOME_LNG, dt.date(2022, 9, 5), 9, "research", 10) for obs_id in range(16, 21)]
    )
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO observations "
            "(id, taxon_id, lat, lng, observed_on, month, quality_grade, "
            "positional_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
    build_phenology(con, CELL)

    from foray.config import Ingest

    return Settings(
        home=Home(name="Home", lat=HOME_LAT, lng=HOME_LNG, radius_km=200),
        cell_deg=CELL,
        ingest=Ingest(since_year=2015, quality_grade="research", recent_weeks=8),
    )


@pytest.fixture
def client(cfg: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(cfg)) as client:
        yield client


@pytest.fixture(autouse=True)
def _no_reverse_geocode_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/api/location` reverse-geocodes server-side (issue #145) when `lat`/`lng` are posted
    without `name` - block the real Nominatim call by default so tests here stay network-free
    per this module's docstring; tests exercising that behavior specifically override this
    locally with their own `monkeypatch.setattr`."""

    def fake_reverse(lat: float, lng: float, *, client: object = None) -> None:
        raise LookupError("network disabled in tests")

    monkeypatch.setattr("foray.api.geocode.reverse", fake_reverse)


@pytest.fixture(autouse=True)
def _no_trail_network_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/api/trails/network` (query param `trail_id`) does a live, node-scoped Overpass query
    (issue: draw the real trail on trailhead selection) when authoritative OSM topology isn't
    already cached - block it by default so tests here stay network-free per this module's
    docstring; this also makes the nearest-cached fallback path deterministic to test rather than
    depending on what the real Overpass API happens to return for a given node id."""

    def fake_network(node_id: int, *, client: object = None) -> None:
        return None

    monkeypatch.setattr("foray.trails.trailhead_network", fake_network)


@pytest.fixture(autouse=True)
def _no_place_geocode_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/api/destinations/{region_id}/place` (issue #206) reverse-geocodes a region's centroid
    on a cache miss - block the real Nominatim call by default, same as `_no_reverse_geocode_network`
    above; tests exercising that behavior specifically override this locally."""

    def fake_notable_place_name(lat: float, lng: float, *, client: object = None) -> None:
        raise httpx.ConnectError("network disabled in tests")

    monkeypatch.setattr("foray.api.geocode.notable_place_name", fake_notable_place_name)


def test_get_config(client: TestClient) -> None:
    response = client.get("/api/config")
    assert response.status_code == 200
    body = response.json()
    assert body["home"]["name"] == "Home"
    assert body["cell_deg"] == CELL
    assert body["refreshing"] is False


def test_get_genera_searches_by_scientific_or_common_name(client: TestClient, con: psycopg.Connection) -> None:
    upsert_fungi_genera(
        con,
        [
            {"taxon_id": 47348, "name": "Cantharellus", "common_name": "Chanterelles", "observations_count": 90000},
            {"taxon_id": 999999, "name": "Obscurella", "common_name": None, "observations_count": 3},
        ],
    )

    response = client.get("/api/genera", params={"q": "chanterelle"})
    assert response.status_code == 200
    # The `client` fixture's own `cfg` fixture also seeds a taxon_id=CHANT "Cantharellus"/
    # "Chanterelles" genus into the shared catalog, so this scientific/common-name search
    # legitimately matches both - check the one this test seeded is among the hits, rather
    # than asserting an exact list (which would be coupled to that unrelated fixture).
    assert {"taxon_id": 47348, "name": "Cantharellus", "common_name": "Chanterelles"} in response.json()

    no_common_name = client.get("/api/genera", params={"q": "obscurella"})
    assert no_common_name.json() == [{"taxon_id": 999999, "name": "Obscurella", "common_name": None}]


def test_selected_genera_empty_for_fresh_device(client: TestClient) -> None:
    client.cookies.set("device_id", "device-genera-fresh0000")
    response = client.get("/api/genera/selected")
    assert response.status_code == 200
    assert response.json() == []


def test_add_and_remove_selected_genus_round_trip(client: TestClient, con: psycopg.Connection) -> None:
    upsert_fungi_genera(con, [{"taxon_id": 47348, "name": "Cantharellus", "common_name": "Chanterelles"}])
    client.cookies.set("device_id", "device-genera-roundtrip")

    added = client.post("/api/genera/47348")
    assert added.status_code == 200
    assert added.json() == {"status": "added"}

    selected = client.get("/api/genera/selected")
    assert selected.json() == [{"taxon_id": 47348, "name": "Cantharellus", "common_name": "Chanterelles"}]

    removed = client.delete("/api/genera/47348")
    assert removed.status_code == 200
    assert removed.json() == {"status": "removed"}
    assert client.get("/api/genera/selected").json() == []


def test_selected_genera_is_scoped_per_device(client: TestClient) -> None:
    client.cookies.set("device_id", "device-genera-aaaaaaaaaa")
    client.post("/api/genera/47348")

    client.cookies.set("device_id", "device-genera-bbbbbbbbbb")
    assert client.get("/api/genera/selected").json() == []


def test_destinations_defaults_to_selected_genera(client: TestClient) -> None:
    """The 'all' default now means this device's picks, or everything if none are picked."""
    client.cookies.set("device_id", "device-genera-filter000")
    client.post(f"/api/genera/{CHANT}")

    response = client.get("/api/destinations", params={"months": "7"})
    assert response.status_code == 200
    body = response.json()
    assert body, "expected at least one ranked region"
    assert all(hit["taxon_id"] == CHANT for region in body for hit in region["species"])


def test_destinations_ranks_morel_region(client: TestClient) -> None:
    response = client.get("/api/destinations", params={"months": "4"})
    assert response.status_code == 200
    body = response.json()
    assert body, "expected at least one ranked region"
    assert body[0]["species"][0]["common_name"] == "Morels"


def test_destinations_carry_elevation_field_null_until_enriched(client: TestClient) -> None:
    # Fixture observations have no elevation_m, so the field is present but null (issue #36).
    body = client.get("/api/destinations", params={"months": "4"}).json()
    assert body and all("elevation_m" in region and region["elevation_m"] is None for region in body)


def test_destinations_report_region_mean_elevation(client: TestClient, con: psycopg.Connection) -> None:
    con.execute("UPDATE observations SET elevation_m = 900 WHERE quality_grade = 'research'")
    build_phenology(con, CELL)
    body = client.get("/api/destinations", params={"months": "4"}).json()
    assert body and all(region["elevation_m"] == 900 for region in body)


def test_destinations_bad_months_is_400(client: TestClient) -> None:
    response = client.get("/api/destinations", params={"months": "not-a-month"})
    assert response.status_code == 400


def test_destinations_out_of_range_month_is_400(client: TestClient) -> None:
    response = client.get("/api/destinations", params={"months": "13"})
    assert response.status_code == 400


def test_calendar_for_ranked_region(client: TestClient) -> None:
    region_id = client.get("/api/destinations", params={"months": "4"}).json()[0]["region_id"]
    response = client.get("/api/calendar", params={"region_id": region_id})
    assert response.status_code == 200
    body = response.json()
    assert "4" in body
    assert body["4"]["species"]["Morchella (Morels)"] == 10


def test_observation_photos_filters_by_license(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    region_id = client.get("/api/destinations", params={"months": "4"}).json()[0]["region_id"]
    obs_id = 1  # a Morel observation id from the `cfg` fixture (ids 1..10), in this region

    def fake_photos(ids: list[int]) -> dict[int, list[dict]]:
        return {
            obs_id: [
                {
                    "url": "https://static.inaturalist.org/photos/1/square.jpg",
                    "license_code": "cc-by",
                    "attribution": "(c) someone",
                },
                {
                    "url": "https://static.inaturalist.org/photos/2/square.jpg",
                    "license_code": "cc-by-nd",
                    "attribution": "(c) someone else",
                },
                {
                    "url": "https://static.inaturalist.org/photos/3/square.jpg",
                    "license_code": None,
                    "attribution": "all rights reserved",
                },
            ]
        }

    monkeypatch.setattr("foray.api.inat.photos_for_observations", fake_photos)
    # Explicit species scope: a device with no genus selection now defaults to "everything
    # nearby" (issue #79 Phase 2), not just this fixture's one configured species.
    response = client.get(
        "/api/observations/photos", params={"region_id": region_id, "species": str(MOREL), "months": "4"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["has_more"] is False
    observations = body["observations"]
    assert len(observations) == 10  # every Morel observation in the fixture is in this region
    target = next(obs for obs in observations if obs["id"] == obs_id)
    assert len(target["photos"]) == 1
    assert target["photos"][0]["license_code"] == "cc-by"
    others = [obs for obs in observations if obs["id"] != obs_id]
    assert all(obs["photos"] == [] for obs in others)


def test_observation_photos_filters_by_month(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("foray.api.inat.photos_for_observations", lambda ids: {})
    region_id = client.get("/api/destinations", params={"months": "4"}).json()[0]["region_id"]
    # Fixture Morels are all observed in April 2022 - a July filter must exclude them.
    response = client.get(
        "/api/observations/photos", params={"region_id": region_id, "species": str(MOREL), "months": "7"}
    )
    assert response.status_code == 200
    assert response.json() == {"observations": [], "has_more": False}


def test_observation_photos_bad_region_returns_empty(client: TestClient) -> None:
    response = client.get("/api/observations/photos", params={"region_id": "999_999"})
    assert response.status_code == 200
    assert response.json() == {"observations": [], "has_more": False}


def test_observation_photos_paginates_with_offset(
    client: TestClient, con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("foray.api.inat.photos_for_observations", lambda ids: {})
    # Fixture already has 10 Morel observations (ids 1-10) in this region/month - add 5 more so
    # the region's total (15) crosses the default page size (12).
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month,"
            " quality_grade, positional_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (obs_id, MOREL, HOME_LAT, HOME_LNG, dt.date(2022, 4, 15), 4, "research", 10)
                for obs_id in range(9001, 9006)
            ],
        )
    region_id = client.get("/api/destinations", params={"months": "4"}).json()[0]["region_id"]

    first_page = client.get(
        "/api/observations/photos", params={"region_id": region_id, "species": str(MOREL), "months": "4"}
    ).json()
    assert len(first_page["observations"]) == 12
    assert first_page["has_more"] is True

    second_page = client.get(
        "/api/observations/photos",
        params={"region_id": region_id, "species": str(MOREL), "months": "4", "offset": 12},
    ).json()
    assert len(second_page["observations"]) == 3
    assert second_page["has_more"] is False

    first_ids = {obs["id"] for obs in first_page["observations"]}
    second_ids = {obs["id"] for obs in second_page["observations"]}
    assert first_ids.isdisjoint(second_ids)


def test_alerts_empty_when_no_recent_observations(client: TestClient) -> None:
    # Fixture observations are dated 2022, well outside the default recent_weeks window.
    response = client.get("/api/alerts")
    assert response.status_code == 200
    assert response.json() == []


def test_precise_observations_empty_by_default(client: TestClient) -> None:
    # Fixture Morels have no `obscured` column set (NULL), so none qualify as known-precise.
    response = client.get("/api/observations/precise", params={"species": str(MOREL), "months": "4"})
    assert response.status_code == 200
    assert response.json() == []


def test_precise_observations_returns_unobscured_only(client: TestClient, con: psycopg.Connection) -> None:
    with con.cursor() as cur:
        cur.execute(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month,"
            " quality_grade, uri, obscured) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false)",
            (9001, MOREL, HOME_LAT, HOME_LNG, dt.date(2022, 4, 15), 4, "research", "https://x/9001"),
        )
        cur.execute(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month,"
            " quality_grade, obscured) VALUES (%s, %s, %s, %s, %s, %s, %s, true)",
            (9002, MOREL, HOME_LAT, HOME_LNG, dt.date(2022, 4, 15), 4, "research"),
        )
    response = client.get("/api/observations/precise", params={"species": str(MOREL), "months": "4"})
    assert response.status_code == 200
    body = response.json()
    assert [obs["id"] for obs in body] == [9001]
    assert body[0]["lat"] == pytest.approx(HOME_LAT)
    assert body[0]["name"] == "Morchella"
    assert body[0]["uri"] == "https://x/9001"


def test_precise_observations_by_latlng(client: TestClient, con: psycopg.Connection) -> None:
    with con.cursor() as cur:
        cur.execute(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month,"
            " quality_grade, uri, obscured) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false)",
            (9003, MOREL, HOME_LAT, HOME_LNG, dt.date(2022, 4, 15), 4, "research", "https://x/9003"),
        )
    response = client.get(
        "/api/observations/precise",
        params={"species": str(MOREL), "months": "4", "lat": HOME_LAT, "lng": HOME_LNG, "radius_km": 5},
    )
    assert response.status_code == 200
    assert [obs["id"] for obs in response.json()] == [9003]


def test_precise_observations_by_latlng_empty(client: TestClient) -> None:
    response = client.get("/api/observations/precise", params={"lat": HOME_LAT, "lng": HOME_LNG})
    assert response.status_code == 200
    assert response.json() == []


def test_precise_observations_requires_lat_lng_together(client: TestClient) -> None:
    response = client.get("/api/observations/precise", params={"lat": HOME_LAT})
    assert response.status_code == 400


def test_camps_requires_region_or_latlng(client: TestClient) -> None:
    response = client.get("/api/camps")
    assert response.status_code == 400


def test_camps_by_latlng_empty(client: TestClient) -> None:
    response = client.get("/api/camps", params={"lat": HOME_LAT, "lng": HOME_LNG})
    assert response.status_code == 200
    assert response.json() == []


def test_camps_limit_caps_the_results(client: TestClient, con: psycopg.Connection) -> None:
    upsert_campsites(
        con,
        [
            ("ridb:1", "Close", "campground", None, None, HOME_LAT + 0.01, HOME_LNG, "ridb", "u1"),
            ("ridb:2", "Far", "campground", None, None, HOME_LAT + 0.05, HOME_LNG, "ridb", "u2"),
        ],
    )
    response = client.get("/api/camps", params={"lat": HOME_LAT, "lng": HOME_LNG, "limit": 1})
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_land_by_latlng_empty(client: TestClient) -> None:
    response = client.get("/api/land", params={"lat": HOME_LAT, "lng": HOME_LNG})
    assert response.status_code == 200
    assert response.json() == []


def test_trails_by_latlng_empty(client: TestClient) -> None:
    response = client.get("/api/trails", params={"lat": HOME_LAT, "lng": HOME_LNG})
    assert response.status_code == 200
    assert response.json() == []


def test_trails_kind_and_limit_scope_the_results(client: TestClient, con: psycopg.Connection) -> None:
    trailhead = _parse_element(
        {"type": "node", "id": 1, "lat": HOME_LAT + 0.01, "lon": HOME_LNG, "tags": {"highway": "trailhead"}}
    )
    path = _parse_element(
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "path", "name": "Ridge"},
            "geometry": [{"lat": HOME_LAT + 0.02, "lon": HOME_LNG}, {"lat": HOME_LAT + 0.03, "lon": HOME_LNG}],
        }
    )
    assert trailhead is not None and path is not None
    upsert_trails(con, [trailhead, path])

    response = client.get("/api/trails", params={"lat": HOME_LAT, "lng": HOME_LNG, "kind": "trailhead"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["kind"] == "trailhead"

    response = client.get("/api/trails", params={"lat": HOME_LAT, "lng": HOME_LNG, "limit": 1})
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_trail_network_404s_for_an_unknown_trailhead(client: TestClient) -> None:
    response = client.get("/api/trails/network", params={"trail_id": "osm:node/999"})
    assert response.status_code == 404


def test_trail_network_falls_back_to_nearest_cached_trail(client: TestClient, con: psycopg.Connection) -> None:
    trailhead = _parse_element(
        {"type": "node", "id": 1, "lat": HOME_LAT, "lon": HOME_LNG, "tags": {"highway": "trailhead"}}
    )
    nearby_path = _parse_element(
        {
            "type": "way",
            "id": 2,
            "tags": {"highway": "path", "name": "Nearby"},
            "geometry": [{"lat": HOME_LAT + 0.001, "lon": HOME_LNG}, {"lat": HOME_LAT + 0.002, "lon": HOME_LNG}],
        }
    )
    assert trailhead is not None and nearby_path is not None
    upsert_trails(con, [trailhead, nearby_path])

    # The autouse `_no_trail_network_lookup` fixture makes the live Overpass lookup return
    # nothing, exercising the nearest-cached fallback exactly like a real "trailhead not
    # topologically linked to any way/relation" case would.
    response = client.get("/api/trails/network", params={"trail_id": "osm:node/1"})
    assert response.status_code == 200
    body = response.json()
    assert body["authoritative"] is False
    assert body["trail"]["name"] == "Nearby"


def test_camps_bad_region_id_is_400(client: TestClient) -> None:
    response = client.get("/api/camps", params={"region_id": "not-a-region-id"})
    assert response.status_code == 400


def test_region_place_bad_region_id_is_400(client: TestClient) -> None:
    response = client.get("/api/destinations/not-a-region-id/place")
    assert response.status_code == 400


def test_region_place_network_failure_returns_null(client: TestClient) -> None:
    # The autouse `_no_place_geocode_network` fixture already makes geocode.notable_place_name
    # raise, exercising the "answer this request with no title, don't cache the failure" path.
    response = client.get("/api/destinations/95_-245/place")
    assert response.status_code == 200
    assert response.json() == {"place_name": None}


def test_region_place_resolves_and_caches(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_notable_place_name(lat: float, lng: float, *, client: object = None) -> str:
        calls.append((lat, lng))
        return "Mt. Hood National Forest"

    monkeypatch.setattr("foray.api.geocode.notable_place_name", fake_notable_place_name)

    first = client.get("/api/destinations/95_-245/place")
    second = client.get("/api/destinations/95_-245/place")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"place_name": "Mt. Hood National Forest"}
    # Only the first request should have actually hit the (fake) geocoder - the second is a
    # cache hit from region_places.
    assert len(calls) == 1


def test_region_place_caches_a_negative_result(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_notable_place_name(lat: float, lng: float, *, client: object = None) -> None:
        calls.append((lat, lng))
        return None

    monkeypatch.setattr("foray.api.geocode.notable_place_name", fake_notable_place_name)

    first = client.get("/api/destinations/95_-245/place")
    second = client.get("/api/destinations/95_-245/place")

    assert first.json() == second.json() == {"place_name": None}
    assert len(calls) == 1


def test_plan_route(client: TestClient) -> None:
    response = client.get("/api/plan", params={"months": "4", "require_free_camp": "false", "max_stops": 1})
    assert response.status_code == 200
    body = response.json()
    assert "stops" in body
    # No destination given -> the server auto-picks one rather than falling back to radial-only.
    assert body["start_lat"] == HOME_LAT
    assert body["start_lng"] == HOME_LNG
    assert body["auto_destination"] is True


def test_plan_route_explicit_start_and_destination(client: TestClient) -> None:
    # "lat,lng" strings resolve without a network call (foray.geocode.resolve short-circuits
    # on a raw coordinate pair), so this covers the new params without mocking Nominatim.
    dest_lat, dest_lng = HOME_LAT + 1.0, HOME_LNG
    response = client.get(
        "/api/plan",
        params={
            "months": "4",
            "require_free_camp": "false",
            "start": f"{HOME_LAT},{HOME_LNG}",
            "destination": f"{dest_lat},{dest_lng}",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["start_lat"] == HOME_LAT
    assert body["destination_lat"] == dest_lat
    assert body["auto_destination"] is False
    assert body["destination_name"] is None


def test_plan_route_bad_destination_is_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolve(query: str) -> None:
        raise LookupError(f"no location found for {query!r}")

    monkeypatch.setattr("foray.api.geocode.resolve", fake_resolve)
    response = client.get("/api/plan", params={"destination": "nowhereville"})
    assert response.status_code == 404


def test_plan_route_geocode_network_failure_is_502_without_leaking_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_resolve(query: str) -> None:
        raise RuntimeError("connection reset by peer at 10.0.0.5:443")

    monkeypatch.setattr("foray.api.geocode.resolve", fake_resolve)
    response = client.get("/api/plan", params={"destination": "somewhere"})
    assert response.status_code == 502
    assert "10.0.0.5" not in response.text
    assert "connection reset" not in response.text


def test_plan_route_auto_pick_uses_device_home_radius(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_plan_route(con, **kwargs):  # noqa: ANN001, ANN003 - test double mirrors scoring.plan_route's signature
        captured.update(kwargs)
        return TripPlan(
            start_lat=kwargs["start_lat"],
            start_lng=kwargs["start_lng"],
            destination_lat=kwargs["start_lat"],
            destination_lng=kwargs["start_lng"],
            destination_name=None,
            auto_destination=True,
            corridor_km=kwargs["corridor_km"],
            months=kwargs["months"],
            n_stops=0,
            total_drive_km=0.0,
            stops=[],
            skipped_unreachable=0,
        )

    monkeypatch.setattr("foray.api.scoring.plan_route", fake_plan_route)
    response = client.get("/api/plan", params={"months": "4"})
    assert response.status_code == 200
    # The device's configured home radius (200, from the `cfg` fixture's Home), not
    # plan_route's own 300km default - auto-pick shouldn't silently ignore it.
    assert captured["auto_pick_radius_km"] == 200


def test_set_location_by_latlng(client: TestClient) -> None:
    response = client.post("/api/location", json={"lat": 40.0, "lng": -105.0, "radius_km": 100})
    assert response.status_code == 200
    body = response.json()
    assert body["home"]["lat"] == 40.0
    assert "needs_refresh" not in body


def test_set_location_returns_home(client: TestClient) -> None:
    response = client.post("/api/location", json={"lat": HOME_LAT, "lng": HOME_LNG, "radius_km": 50})
    assert response.status_code == 200
    body = response.json()
    assert body["home"]["lat"] == HOME_LAT
    assert "needs_refresh" not in body


def test_set_location_requires_query_or_latlng(client: TestClient) -> None:
    response = client.post("/api/location", json={})
    assert response.status_code == 400


def test_set_location_latlng_reverse_geocodes_when_name_omitted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from foray.geocode import Location

    def fake_reverse(lat: float, lng: float, *, client: object = None) -> Location:
        return Location(name="Boulder, Colorado, USA", lat=lat, lng=lng)

    monkeypatch.setattr("foray.api.geocode.reverse", fake_reverse)
    response = client.post("/api/location", json={"lat": 40.0, "lng": -105.0})
    assert response.status_code == 200
    assert response.json()["home"]["name"] == "Boulder, Colorado, USA"


def test_set_location_latlng_falls_back_to_coords_when_reverse_geocode_fails(client: TestClient) -> None:
    # The autouse `_no_reverse_geocode_network` fixture already makes `geocode.reverse` raise -
    # this asserts the fallback behavior that failure should produce, not just that it's mocked.
    response = client.post("/api/location", json={"lat": 40.0, "lng": -105.0})
    assert response.status_code == 200
    assert response.json()["home"]["name"] == "40.0000, -105.0000"


def test_set_location_latlng_client_name_skips_reverse_geocode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_called(lat: float, lng: float, *, client: object = None) -> None:
        raise AssertionError("geocode.reverse should not be called when a name is supplied")

    monkeypatch.setattr("foray.api.geocode.reverse", fail_if_called)
    response = client.post("/api/location", json={"lat": 40.0, "lng": -105.0, "name": "My spot"})
    assert response.status_code == 200
    assert response.json()["home"]["name"] == "My spot"


def test_config_sets_device_id_cookie_on_first_visit(client: TestClient) -> None:
    response = client.get("/api/config")
    assert response.status_code == 200
    assert "device_id" in response.cookies


def test_config_does_not_set_cookie_when_device_id_already_present(client: TestClient) -> None:
    client.cookies.set("device_id", "existing-device-id")
    response = client.get("/api/config")
    assert response.status_code == 200
    assert "device_id" not in response.cookies


def test_config_falls_back_to_default_home_for_unknown_device(client: TestClient) -> None:
    client.cookies.set("device_id", "never-seen-before")
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["home"]["name"] == "Home"


def test_config_rejects_malformed_device_id_cookie(client: TestClient) -> None:
    """A hand-crafted/too-short cookie value is treated as absent, not trusted as-is."""
    client.cookies.set("device_id", "too-short")
    response = client.get("/api/config")
    assert response.status_code == 200
    assert "device_id" in response.cookies
    assert response.cookies["device_id"] != "too-short"


def test_config_cookie_is_secure_behind_https_proxy(client: TestClient) -> None:
    """Cloudflare terminates TLS and proxies over plain HTTP; trust X-Forwarded-Proto for Secure."""
    response = client.get("/api/config", headers={"X-Forwarded-Proto": "https"})
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "; secure" in set_cookie.lower()
    assert "strict-transport-security" in response.headers


def test_config_cookie_is_not_secure_over_plain_http(client: TestClient) -> None:
    """Local dev (no proxy in front) should still get a cookie that persists over plain HTTP."""
    response = client.get("/api/config")
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "; secure" not in set_cookie.lower()
    # HSTS over plain HTTP is a no-op for browsers and just confusing to send - Copilot
    # review caught this: only emit it when the client-facing scheme is actually HTTPS.
    assert "strict-transport-security" not in response.headers


def test_location_is_scoped_per_device(client: TestClient) -> None:
    """Two different device-id cookies must not see or stomp each other's saved home."""
    client.cookies.set("device_id", "test-device-aaaaaaaaaaaaaaaaaaaa")
    set_a = client.post("/api/location", json={"lat": 10.0, "lng": 20.0, "radius_km": 50})
    assert set_a.status_code == 200
    assert set_a.json()["home"]["lat"] == 10.0

    client.cookies.set("device_id", "test-device-bbbbbbbbbbbbbbbbbbbb")
    set_b = client.post("/api/location", json={"lat": 30.0, "lng": 40.0, "radius_km": 75})
    assert set_b.status_code == 200
    assert set_b.json()["home"]["lat"] == 30.0

    # Device A's saved home is unaffected by device B's write.
    client.cookies.set("device_id", "test-device-aaaaaaaaaaaaaaaaaaaa")
    get_a = client.get("/api/config")
    assert get_a.json()["home"]["lat"] == 10.0
    assert get_a.json()["home"]["radius_km"] == 50

    client.cookies.set("device_id", "test-device-bbbbbbbbbbbbbbbbbbbb")
    get_b = client.get("/api/config")
    assert get_b.json()["home"]["lat"] == 30.0
    assert get_b.json()["home"]["radius_km"] == 75

    # A device that never saved a location still gets the default, not another device's home.
    client.cookies.set("device_id", "test-device-cccccccccccccccccc")
    get_unknown = client.get("/api/config")
    assert get_unknown.json()["home"]["name"] == "Home"


def test_delete_location_reverts_to_default_home(client: TestClient) -> None:
    """Issue #81: a visitor can delete their saved override outright."""
    client.cookies.set("device_id", "test-device-cccccccccccccccccc")
    set_response = client.post("/api/location", json={"lat": 10.0, "lng": 20.0, "radius_km": 50})
    assert set_response.status_code == 200

    delete_response = client.delete("/api/location")
    assert delete_response.status_code == 200
    assert delete_response.json()["status"] == "deleted"

    get_response = client.get("/api/config")
    assert get_response.json()["home"]["name"] == "Home"


def test_delete_location_is_scoped_per_device(client: TestClient) -> None:
    """Deleting device A's saved location must not touch device B's."""
    client.cookies.set("device_id", "test-device-dddddddddddddddddd")
    client.post("/api/location", json={"lat": 10.0, "lng": 20.0, "radius_km": 50})

    client.cookies.set("device_id", "test-device-eeeeeeeeeeeeeeeeeeee")
    client.post("/api/location", json={"lat": 30.0, "lng": 40.0, "radius_km": 75})
    client.delete("/api/location")

    client.cookies.set("device_id", "test-device-dddddddddddddddddd")
    get_response = client.get("/api/config")
    assert get_response.json()["home"]["lat"] == 10.0


def test_post_location_rejects_oversized_body(client: TestClient) -> None:
    """Issue #82: app-level backstop below Cloudflare's 100MB edge cap."""
    oversized_query = "x" * (64 * 1024)
    response = client.post("/api/location", json={"query": oversized_query})
    assert response.status_code == 413


def test_destinations_uses_per_device_home(client: TestClient) -> None:
    """A device with no saved override still ranks by the default home (existing behavior)."""
    client.cookies.set("device_id", "device-destinations")
    response = client.get("/api/destinations", params={"months": "4"})
    assert response.status_code == 200
    body = response.json()
    assert body, "expected at least one ranked region"


def test_refresh_rejects_unknown_target(client: TestClient) -> None:
    response = client.post("/api/refresh", params={"target": "bogus"})
    assert response.status_code == 400


def test_cancel_refresh_when_idle(client: TestClient) -> None:
    response = client.delete("/api/refresh")
    assert response.status_code == 200
    assert response.json() == {"status": "idle"}


def _wait_for_idle(client: TestClient) -> None:
    for _ in range(100):
        if not client.get("/api/config").json()["refreshing"]:
            return
        time.sleep(0.05)
    pytest.fail("refresh did not finish in time")


def test_refresh_rate_limits_repeat_triggers_from_same_ip(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A client can't hammer /api/refresh (and the upstream iNat/RIDB calls behind it)."""
    monkeypatch.setattr("foray.api.ingest", lambda *args, **kwargs: None)
    monkeypatch.setattr("foray.scoring.build_phenology", lambda *args, **kwargs: None)

    started = client.post("/api/refresh", params={"target": "mushrooms"})
    assert started.status_code == 200
    _wait_for_idle(client)

    again = client.post("/api/refresh", params={"target": "mushrooms"})
    assert again.status_code == 429
    assert "retry-after" in {key.lower() for key in again.headers}


def test_refresh_rate_limit_is_scoped_per_client_ip(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cloudflare's CF-Connecting-IP is trusted for the rate-limit key, not a shared bucket."""
    monkeypatch.setattr("foray.api.ingest", lambda *args, **kwargs: None)
    monkeypatch.setattr("foray.scoring.build_phenology", lambda *args, **kwargs: None)

    started = client.post("/api/refresh", params={"target": "mushrooms"}, headers={"cf-connecting-ip": "10.0.0.1"})
    assert started.status_code == 200
    _wait_for_idle(client)

    other_ip = client.post("/api/refresh", params={"target": "mushrooms"}, headers={"cf-connecting-ip": "10.0.0.2"})
    assert other_ip.status_code == 200
    _wait_for_idle(client)


def test_refresh_ignores_malformed_cf_connecting_ip(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bogus CF-Connecting-IP value must not let a caller dodge the rate limit."""
    monkeypatch.setattr("foray.api.ingest", lambda *args, **kwargs: None)
    monkeypatch.setattr("foray.scoring.build_phenology", lambda *args, **kwargs: None)

    bogus_ip = "not-an-ip"
    started = client.post("/api/refresh", params={"target": "mushrooms"}, headers={"cf-connecting-ip": bogus_ip})
    assert started.status_code == 200
    _wait_for_idle(client)

    again = client.post("/api/refresh", params={"target": "mushrooms"}, headers={"cf-connecting-ip": bogus_ip})
    assert again.status_code == 429


def test_refresh_concurrent_requests_start_only_one(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for #88: concurrent POSTs must not both pass the check-then-act guard and
    launch overlapping refresh threads."""
    call_count = 0
    call_lock = threading.Lock()
    release = threading.Event()

    def fake_ingest(cfg: Settings, db: psycopg.Connection, **kwargs: object) -> None:
        nonlocal call_count
        with call_lock:
            call_count += 1
        # Block until every concurrent request has had a chance to race the guard, so the
        # window the original bug needed to slip through is actually exercised.
        release.wait(timeout=5)

    monkeypatch.setattr("foray.api.ingest", fake_ingest)
    monkeypatch.setattr("foray.scoring.build_phenology", lambda *args, **kwargs: None)

    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def fire() -> None:
        response = client.post("/api/refresh", params={"target": "mushrooms"})
        with statuses_lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=fire) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    release.set()
    _wait_for_idle(client)

    assert call_count == 1, "ingest ran more than once - the check-then-act race reopened"
    assert statuses == [200] * 5


def test_refresh_ingests_around_calling_devices_home(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: refresh must use the calling device's saved home, not the env-default home."""
    captured_homes: list[Home] = []

    def fake_ingest(cfg: Settings, db: psycopg.Connection, **kwargs: object) -> None:
        captured_homes.append(cfg.home)

    monkeypatch.setattr("foray.api.ingest", fake_ingest)
    # The assertion only cares which Home was threaded into ingest - stub out the real
    # phenology rebuild (DDL + aggregation) too, so the test doesn't depend on that work
    # finishing within the polling window below on a slower machine/CI.
    monkeypatch.setattr("foray.scoring.build_phenology", lambda *args, **kwargs: None)

    client.cookies.set("device_id", "test-device-refresh-own-home")
    saved = client.post(
        "/api/location",
        json={"lat": 40.0, "lng": -105.0, "name": "Boulder", "radius_km": 50},
    )
    assert saved.status_code == 200

    started = client.post("/api/refresh", params={"target": "mushrooms"})
    assert started.status_code == 200
    assert started.json() == {"status": "started"}

    for _ in range(100):
        if not client.get("/api/config").json()["refreshing"]:
            break
        time.sleep(0.05)
    else:
        pytest.fail("refresh did not finish in time")

    assert len(captured_homes) == 1
    assert captured_homes[0].name == "Boulder"
    assert captured_homes[0].lat == 40.0
    assert captured_homes[0].lng == -105.0


def test_index_serves_built_frontend_or_hint(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code in (200, 503)
    assert "Foray Planner" in response.text or "<!doctype html>" in response.text.lower()


def test_get_coverage_reports_latest_run_not_a_cumulative_sum(client: TestClient, con: psycopg.Connection) -> None:
    """issue #79 Phase 4: ingest_region() writes one obs:fungi:place:{id}:{window} row per
    incremental run (overlapping windows), plus a pre-Phase-4 database may still carry old
    per-taxon obs:{taxon_id}:place:{id}:{window} rows. Neither should be summed together -
    the response should reflect only the latest obs:fungi run for that region."""
    place_id = 46  # Washington, in Settings' default 50-state coverage list
    con.execute(
        "INSERT INTO ingest_log (key, fetched_at, row_count) VALUES (%s, now() - interval '1 day', %s)",
        [f"obs:fungi:place:{place_id}:2024-01-01:2024-06-01", 100],
    )
    con.execute(
        "INSERT INTO ingest_log (key, fetched_at, row_count) VALUES (%s, now(), %s)",
        [f"obs:fungi:place:{place_id}:2024-05-25:2024-06-08", 40],
    )
    # A legacy pre-Phase-4 per-taxon key for the same place - must not be counted at all.
    con.execute(
        "INSERT INTO ingest_log (key, fetched_at, row_count) VALUES (%s, now(), %s)",
        [f"obs:111:place:{place_id}:2024-01-01:2024-06-01", 9999],
    )

    response = client.get("/api/coverage")
    assert response.status_code == 200
    body = response.json()
    washington = next(region for region in body if region["place_id"] == place_id)
    assert washington["observations_ingested"] == 40
