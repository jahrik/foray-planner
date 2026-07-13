"""FastAPI route tests over the shared test Postgres (no network beyond it, per python skill)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient

from foray.api import create_app
from foray.config import Home, Settings, Species
from foray.scoring import build_phenology

CELL = 0.5
MOREL = 111
HOME_LAT, HOME_LNG = 47.6, -122.3


@pytest.fixture
def cfg(con: psycopg.Connection) -> Settings:
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO taxa VALUES (%s, %s, %s, %s)",
            [(MOREL, "Morchella", "Morels", "genus")],
        )
    rows = [
        (obs_id, MOREL, HOME_LAT, HOME_LNG, dt.date(2022, 4, 15), 4, 2022, "research", 10)
        for obs_id in range(1, 21)
    ]
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
    assert body["4"]["species"]["Morels"] == 20


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
    response = client.get(
        "/api/plan", params={"months": "4", "require_free_camp": "false", "max_stops": 1}
    )
    assert response.status_code == 200
    body = response.json()
    assert "stops" in body


def test_set_location_by_latlng(client: TestClient) -> None:
    response = client.post("/api/location", json={"lat": 40.0, "lng": -105.0, "radius_km": 100})
    assert response.status_code == 200
    body = response.json()
    assert body["home"]["lat"] == 40.0
    assert body["needs_refresh"] is True  # no ingested data for the new area yet


def test_set_location_requires_query_or_latlng(client: TestClient) -> None:
    response = client.post("/api/location", json={})
    assert response.status_code == 400


def test_refresh_rejects_unknown_target(client: TestClient) -> None:
    response = client.post("/api/refresh", params={"target": "bogus"})
    assert response.status_code == 400


def test_cancel_refresh_when_idle(client: TestClient) -> None:
    response = client.delete("/api/refresh")
    assert response.status_code == 200
    assert response.json() == {"status": "idle"}


def test_index_serves_built_frontend_or_hint(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code in (200, 503)
    assert "Foray Planner" in response.text or "<!doctype html>" in response.text.lower()
