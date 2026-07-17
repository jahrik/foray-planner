"""FastAPI route tests over the shared test Postgres (no network beyond it, per python skill)."""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from foray.api import create_app
from foray.config import Config, Home, Settings, Species
from foray.scoring import build_phenology

CELL = 0.5
MOREL = 111
CHANT = 222
BOLET = 333
HOME_LAT, HOME_LNG = 47.6, -122.3


@pytest.fixture
def cfg(con: psycopg.Connection) -> Settings:
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO taxa VALUES (%s, %s, %s, %s)",
            [
                (MOREL, "Morchella", "Morels", "genus"),
                (CHANT, "Cantharellus", "Chanterelles", "genus"),
                (BOLET, "Boletus", "King Boletes", "genus"),
            ],
        )
    rows = (
        [(obs_id, MOREL, HOME_LAT, HOME_LNG, dt.date(2022, 4, 15), 4, 2022, "research", 10) for obs_id in range(1, 11)]
        + [
            (obs_id, CHANT, HOME_LAT, HOME_LNG, dt.date(2022, 7, 10), 7, 2022, "research", 10)
            for obs_id in range(11, 16)
        ]
        + [
            (obs_id, BOLET, HOME_LAT, HOME_LNG, dt.date(2022, 9, 5), 9, 2022, "research", 10)
            for obs_id in range(16, 21)
        ]
    )
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO observations "
            "(id, taxon_id, lat, lng, observed_on, month, year, quality_grade, "
            "positional_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
    build_phenology(con, CELL)

    from foray.config import Ingest

    return Settings(
        home=Home(name="Home", lat=HOME_LAT, lng=HOME_LNG, radius_km=200),
        cell_deg=CELL,
        ingest=Ingest(since_year=2015, quality_grade="research", recent_weeks=8),
        species=[Species(taxon_id=MOREL, name="Morchella", common_name="Morels", rank="genus")],
    )


@pytest.fixture
def client(cfg: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(cfg)) as client:
        yield client


def test_get_config(client: TestClient) -> None:
    response = client.get("/api/config")
    assert response.status_code == 200
    body = response.json()
    assert body["home"]["name"] == "Home"
    assert body["cell_deg"] == CELL
    assert body["refreshing"] is False


def test_get_species(client: TestClient) -> None:
    response = client.get("/api/species")
    assert response.status_code == 200
    body = response.json()
    assert body == [
        {
            "taxon_id": MOREL,
            "name": "Morchella",
            "common_name": "Morels",
            "rank": "genus",
            "inat_url": f"https://www.inaturalist.org/taxa/{MOREL}",
        }
    ]


def test_destinations_ranks_morel_region(client: TestClient) -> None:
    response = client.get("/api/destinations", params={"months": "4"})
    assert response.status_code == 200
    body = response.json()
    assert body, "expected at least one ranked region"
    assert body[0]["species"][0]["common_name"] == "Morels"


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
    assert body["4"]["species"]["Morels"] == 10


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
    response = client.get("/api/observations/photos", params={"region_id": region_id})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 10  # every Morel observation in the fixture is in this region
    target = next(obs for obs in body if obs["id"] == obs_id)
    assert len(target["photos"]) == 1
    assert target["photos"][0]["license_code"] == "cc-by"
    others = [obs for obs in body if obs["id"] != obs_id]
    assert all(obs["photos"] == [] for obs in others)


def test_observation_photos_bad_region_returns_empty(client: TestClient) -> None:
    response = client.get("/api/observations/photos", params={"region_id": "999_999"})
    assert response.status_code == 200
    assert response.json() == []


def test_alerts_empty_when_no_recent_observations(client: TestClient) -> None:
    # Fixture observations are dated 2022, well outside the default recent_weeks window.
    response = client.get("/api/alerts")
    assert response.status_code == 200
    assert response.json() == []


def test_camps_requires_region_or_latlng(client: TestClient) -> None:
    response = client.get("/api/camps")
    assert response.status_code == 400


def test_camps_by_latlng_empty(client: TestClient) -> None:
    response = client.get("/api/camps", params={"lat": HOME_LAT, "lng": HOME_LNG})
    assert response.status_code == 200
    assert response.json() == []


def test_land_by_latlng_empty(client: TestClient) -> None:
    response = client.get("/api/land", params={"lat": HOME_LAT, "lng": HOME_LNG})
    assert response.status_code == 200
    assert response.json() == []


def test_trails_by_latlng_empty(client: TestClient) -> None:
    response = client.get("/api/trails", params={"lat": HOME_LAT, "lng": HOME_LNG})
    assert response.status_code == 200
    assert response.json() == []


def test_camps_bad_region_id_is_400(client: TestClient) -> None:
    response = client.get("/api/camps", params={"region_id": "not-a-region-id"})
    assert response.status_code == 400


def test_plan_route(client: TestClient) -> None:
    response = client.get("/api/plan", params={"months": "4", "require_free_camp": "false", "max_stops": 1})
    assert response.status_code == 200
    body = response.json()
    assert "stops" in body


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

    def fake_ingest(cfg: Config, db: psycopg.Connection, **kwargs: object) -> None:
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

    def fake_ingest(cfg: Config, db: psycopg.Connection, **kwargs: object) -> None:
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
