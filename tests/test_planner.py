"""Trip-planner tests on hand-built fixtures - no network (per python skill: hermetic)."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from foray.cache import upsert_campsites
from foray.scoring import build_phenology, plan_route

CELL = 0.5
MOREL = 111

# Home near (44.0, -121.0). Three morel regions at increasing distance, all active in October,
# with decreasing observation counts so their score order is deterministic (near = strongest).
HOME_LAT, HOME_LNG = 44.0, -121.0
NEAR = (44.2, -121.0)  # ~22 km N of home
MID = (45.0, -121.0)  # ~111 km N of home
FAR = (47.0, -121.0)  # ~333 km N of home


@pytest.fixture(autouse=True)
def _seed(con: psycopg.Connection) -> None:
    con.execute("INSERT INTO taxa VALUES (%s, %s, %s, %s)", (MOREL, "Morchella", "Morels", "genus"))

    rows: list[tuple] = []
    obs_id = 1
    # More observations closer in, so score (and thus selection order) tracks distance here.
    for (lat, lng), obs_count in ((NEAR, 40), (MID, 25), (FAR, 15)):
        for _ in range(obs_count):
            rows.append((obs_id, MOREL, lat, lng, dt.date(2022, 10, 15), 10, 2022, "research", 10))
            obs_id += 1
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO observations VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", rows
        )
    build_phenology(con, CELL)

    # A free camp beside NEAR and MID; FAR gets only a paid camp.
    upsert_campsites(
        con,
        [
            ("osm:1", "Free NEAR", "dispersed", None, True, NEAR[0], NEAR[1], "osm", "u"),
            ("osm:2", "Free MID", "dispersed", None, True, MID[0], MID[1], "osm", "u"),
            ("ridb:3", "Paid FAR", "campground", "$20", None, FAR[0], FAR[1], "ridb", "u"),
        ],
    )


def _kwargs(**overrides: object) -> dict:
    base: dict = {
        "months": [10],
        "taxon_ids": [MOREL],
        "home_lat": HOME_LAT,
        "home_lng": HOME_LNG,
        "radius_km": 500,
        "cell_deg": CELL,
    }
    base.update(overrides)
    return base


def test_plan_orders_stops_nearest_first_from_home(con: psycopg.Connection) -> None:
    trip = plan_route(con, **_kwargs(require_free_camp=False))
    # NEAR, MID, FAR are all viable; nearest-neighbour from home visits them in distance order.
    assert [s.order for s in trip.stops] == [1, 2, 3]
    assert trip.stops[0].drive_km_from_prev < trip.stops[1].drive_km_from_prev
    # Cumulative drive is monotonic and matches the reported total.
    assert trip.stops[-1].cumulative_drive_km == trip.total_drive_km
    assert trip.n_stops == 3


def test_require_free_camp_drops_paid_only_stops(con: psycopg.Connection) -> None:
    trip = plan_route(con, **_kwargs(require_free_camp=True))
    ids = {s.region_id for s in trip.stops}
    # FAR has only a paid camp -> excluded; NEAR and MID keep their free camp.
    assert len(trip.stops) == 2
    assert all(s.camp_is_free and s.camp is not None for s in trip.stops)
    far_region = f"{int(FAR[0] / CELL)}_{int(FAR[1] / CELL)}"
    assert far_region not in ids


def test_any_camp_annotates_nearest_paid_camp(con: psycopg.Connection) -> None:
    trip = plan_route(con, **_kwargs(require_free_camp=False))
    far = next(s for s in trip.stops if s.camp is not None and not s.camp_is_free)
    assert far.camp is not None and far.camp.name == "Paid FAR"


def test_max_stops_caps_the_itinerary(con: psycopg.Connection) -> None:
    trip = plan_route(con, **_kwargs(require_free_camp=False, max_stops=2))
    assert trip.n_stops == 2
    # The two highest-scoring regions (NEAR, MID) are kept, not FAR.
    assert {s.region_id for s in trip.stops} == {
        f"{int(NEAR[0] / CELL)}_{int(NEAR[1] / CELL)}",
        f"{int(MID[0] / CELL)}_{int(MID[1] / CELL)}",
    }


def test_max_drive_km_reports_unreachable_stops(con: psycopg.Connection) -> None:
    # 150 km legs reach NEAR (~22) and MID (~89 from NEAR) but not FAR (~222 from MID).
    trip = plan_route(con, **_kwargs(require_free_camp=False, max_drive_km=150))
    assert trip.n_stops == 2
    assert trip.skipped_unreachable == 1


def test_empty_when_no_data_returns_empty_plan(con: psycopg.Connection) -> None:
    # A month with no activity yields no candidates and an empty (not error) plan.
    trip = plan_route(con, **_kwargs(months=[1]))
    assert trip.stops == []
    assert trip.n_stops == 0
    assert trip.total_drive_km == 0.0
