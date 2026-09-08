"""Access signals in ranking (issue #306): region_access distances, the trailhead boost and
the remote-region penalty."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from foray.cache import upsert_campsites, upsert_trails
from foray.scoring import rank_destinations, region_access
from foray.scoring.regions import build_phenology

CELL = 0.25
BOLETUS = 48701
LAT, LNG = 44.0, -121.0


@pytest.fixture(autouse=True)
def _seed(con: psycopg.Connection) -> None:
    with con.cursor() as cur:
        cur.execute(
            "INSERT INTO fungi_genera (taxon_id, name, common_name) VALUES (%s, %s, %s)",
            [BOLETUS, "Boletus", "Porcini"],
        )
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, quality_grade)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'research')",
            [(i, BOLETUS, LAT, LNG, dt.date(2022, 9, 15), 9) for i in range(1, 13)],
        )
    build_phenology(con, CELL)


def _trailhead(node: int, lat: float, lng: float) -> tuple[object, ...]:
    point = f'{{"type":"Point","coordinates":[{lng},{lat}]}}'
    return (f"osm:node/{node}", "TH", "trailhead", "osm", "u", lat, lng, point, None, None, None)


def _rank(con: psycopg.Connection) -> list:
    return rank_destinations(
        con, months=[9], taxon_ids=[BOLETUS], home_lat=LAT, home_lng=LNG, radius_km=200, cell_deg=CELL
    )


def test_region_access_reports_nearest_trailhead_and_camp(con: psycopg.Connection) -> None:
    upsert_trails(con, [_trailhead(1, LAT + 0.01, LNG)])  # ~1 km N of the cell centre
    upsert_campsites(
        con, [("ridb:1", "Camp", "campground", None, True, LAT, LNG + 0.05, "ridb", "u", None, None, None)]
    )
    access = region_access(con, [("r", LAT, LNG)])
    th_km, camp_km, camp_free = access["r"]
    assert th_km is not None and th_km < 2.0
    assert camp_km is not None and camp_free is True


def test_close_trailhead_boosts_the_region(con: psycopg.Connection) -> None:
    baseline = _rank(con)[0].score
    upsert_trails(con, [_trailhead(1, LAT, LNG)])  # right on the hotspot
    boosted = _rank(con)[0]
    assert boosted.score > baseline
    assert boosted.trailhead_km is not None and boosted.trailhead_km < 1.0


def test_remote_region_with_no_access_is_penalised(con: psycopg.Connection) -> None:
    baseline = _rank(con)[0].score
    # A trailhead and a camp far outside ACCESS_FAR_KM (~15 km) of the hotspot.
    upsert_trails(con, [_trailhead(9, LAT + 1.0, LNG + 1.0)])
    upsert_campsites(
        con, [("ridb:9", "Far", "campground", None, None, LAT + 1.0, LNG + 1.0, "ridb", "u", None, None, None)]
    )
    penalised = _rank(con)[0]
    assert penalised.score < baseline
    assert penalised.trailhead_km is not None and penalised.trailhead_km > 15.0
