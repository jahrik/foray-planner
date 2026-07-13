"""Scoring tests on hand-built fixtures - no network (per python skill: hermetic)."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from foray.scoring import (
    alerts,
    build_phenology,
    haversine_km,
    place_calendar,
    rank_destinations,
)

CELL = 0.5

# Two well-separated regions:
#   APR region ~ (47.6, -122.3): morels in April.
#   OCT region ~ (44.0, -121.0): chanterelles in October.
MOREL, CHANTERELLE = 111, 222
APR_LAT, APR_LNG = 47.6, -122.3
OCT_LAT, OCT_LNG = 44.0, -121.0


@pytest.fixture(autouse=True)
def _seed(con: psycopg.Connection) -> None:
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO taxa VALUES (%s, %s, %s, %s)",
            [
                (MOREL, "Morchella", "Morels", "genus"),
                (CHANTERELLE, "Cantharellus", "Chanterelles", "genus"),
            ],
        )
    rows: list[tuple] = []
    obs_id = 1
    # 20 morel obs in the APR region, all in April.
    for _ in range(20):
        rows.append(
            (obs_id, MOREL, APR_LAT, APR_LNG, dt.date(2022, 4, 15), 4, 2022, "research", 10)
        )
        obs_id += 1
    # 30 chanterelle obs in the OCT region, all in October.
    for _ in range(30):
        rows.append(
            (obs_id, CHANTERELLE, OCT_LAT, OCT_LNG, dt.date(2022, 10, 15), 10, 2022, "research", 10)
        )
        obs_id += 1
    # A couple stray off-season morels in the OCT region (noise).
    for _ in range(2):
        rows.append((obs_id, MOREL, OCT_LAT, OCT_LNG, dt.date(2022, 7, 1), 7, 2022, "research", 10))
        obs_id += 1
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, year,"
            " quality_grade, positional_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
    build_phenology(con, CELL)


def test_april_ranks_morel_region_first(con: psycopg.Connection) -> None:
    ranked = rank_destinations(
        con,
        months=[4],
        taxon_ids=[MOREL, CHANTERELLE],
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=500,
        cell_deg=CELL,
    )
    assert ranked, "expected at least one region"
    top = ranked[0]
    assert abs(top.center_lat - APR_LAT) < CELL
    assert top.species[0].common_name == "Morels"
    assert top.score_norm == 1.0


def test_october_ranks_chanterelle_region_first(con: psycopg.Connection) -> None:
    ranked = rank_destinations(
        con,
        months=[10],
        taxon_ids=[MOREL, CHANTERELLE],
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=500,
        cell_deg=CELL,
    )
    top = ranked[0]
    assert abs(top.center_lat - OCT_LAT) < CELL
    assert top.species[0].common_name == "Chanterelles"


def test_radius_filters_far_regions(con: psycopg.Connection) -> None:
    # Home right on the APR region, tiny radius -> OCT region excluded.
    ranked = rank_destinations(
        con,
        months=[4, 10],
        taxon_ids=[MOREL, CHANTERELLE],
        home_lat=APR_LAT,
        home_lng=APR_LNG,
        radius_km=50,
        cell_deg=CELL,
    )
    assert all(abs(region.center_lat - APR_LAT) < CELL for region in ranked)


def test_place_calendar_peaks_in_expected_month(con: psycopg.Connection) -> None:
    # Find the OCT region id via the regions table.
    row = con.execute(
        "SELECT region_id FROM regions ORDER BY abs(center_lat - %s) LIMIT 1", [OCT_LAT]
    ).fetchone()
    assert row is not None
    region_id = row[0]
    calendar = place_calendar(con, region_id=region_id, taxon_ids=[MOREL, CHANTERELLE])
    peak_month = max(calendar, key=lambda month: calendar[month]["total"])
    assert peak_month == 10
    assert calendar[10]["species"]["Chanterelles"] == 30


def test_alerts_only_recent(con: psycopg.Connection) -> None:
    # Fixture observations are from 2022 -> nothing within the trailing window.
    active = alerts(
        con,
        taxon_ids=[MOREL, CHANTERELLE],
        home_lat=46.0,
        home_lng=-121.6,
        radius_km=500,
        cell_deg=CELL,
        weeks=4,
    )
    assert active == []


def test_haversine_known_distance() -> None:
    # Seattle -> Portland is ~233 km.
    distance = haversine_km(47.6062, -122.3321, 45.5152, -122.6784)
    assert 220 < distance < 245
