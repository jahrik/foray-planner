"""Fire signals in ranking (issue #227): fire_near distance filter, the active-fire penalty,
and the Morchella-scoped burn-scar boost."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, LiteralString, cast

import psycopg
import pytest

from foray.scoring import build_phenology, fire_near, rank_destinations
from foray.scoring.models import RegionScore

CELL = 0.25
MORCHELLA, OTHER = 47143, 55555
LAT, LNG = 44.0, -121.0
THIS_YEAR = dt.date.today().year


@pytest.fixture(autouse=True)
def _seed(con: psycopg.Connection) -> None:
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO fungi_genera (taxon_id, name, common_name) VALUES (%s, %s, %s)",
            [(MORCHELLA, "Morchella", "Morels"), (OTHER, "Cantharellus", "Chanterelles")],
        )
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, quality_grade)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'research')",
            [(i, MORCHELLA if i % 2 else OTHER, LAT, LNG, dt.date(2022, 5, 15), 5) for i in range(1, 21)],
        )
    build_phenology(con, CELL)


def _add_fire(con: psycopg.Connection, **kw: Any) -> None:
    # The bbox columns are gone (issue #268 PR 5); callers still pass min/max lat-lng to shape
    # the seeded polygon's extent, so pop them out here and use them only for the geometry.
    south = float(kw.pop("min_lat", LAT - 0.2))
    north = float(kw.pop("max_lat", LAT + 0.2))
    west = float(kw.pop("min_lng", LNG - 0.2))
    east = float(kw.pop("max_lng", LNG + 0.2))
    cols = {
        "id": "f1",
        "source_key": "wfigs_active",
        "status": "active",
        "fire_year": THIS_YEAR,
        "dominant_severity": None,
        "is_point": False,
        "incident_url": "http://x",
        "gis_acres": 100.0,
        "percent_contained": 0.0,
        "center_lat": LAT,
        "center_lng": LNG,
        **kw,
    }
    # The geom trigger derives `fire_perimeters.geom` from `geojson` (as the real ingest does);
    # fire_near's ST_DWithin needs it. Default to a perimeter polygon matching `is_point=False`;
    # an `is_point=True` caller gets a center point.
    if "geojson" not in cols:
        if cols["is_point"]:
            cols["geojson"] = json.dumps({"type": "Point", "coordinates": [cols["center_lng"], cols["center_lat"]]})
        else:
            cols["geojson"] = json.dumps(
                {
                    "type": "Polygon",
                    "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
                }
            )
    query = cast(
        LiteralString,
        f"INSERT INTO fire_perimeters ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
    )
    con.execute(query, list(cols.values()))


def _rank(con: psycopg.Connection, taxa: list[int]) -> list[RegionScore]:
    return rank_destinations(con, months=[5], taxon_ids=taxa, home_lat=LAT, home_lng=LNG, radius_km=200, cell_deg=CELL)


def test_fire_near_filters_by_distance(con: psycopg.Connection) -> None:
    _add_fire(con, id="near", center_lat=LAT, center_lng=LNG)
    _add_fire(con, id="far", center_lat=LAT + 5, center_lng=LNG, min_lat=LAT + 4.8, max_lat=LAT + 5.2)
    got = fire_near(con, lat=LAT, lng=LNG, radius_km=50)
    assert [f.id for f in got] == ["near"]


def test_fire_nearby_distance_is_region_relative(con: psycopg.Connection) -> None:
    # A second region ~65 km NE of the first. `_apply_fire` fetches fires from the *centroid*
    # of all regions, so a fire sitting on region 2 must have its distance re-measured to
    # region 2 (~0), not left at the centroid distance (~30+ km).
    far_lat, far_lng = LAT + 0.5, LNG + 0.5
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month, quality_grade)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'research')",
            [(100 + i, MORCHELLA, far_lat, far_lng, dt.date(2022, 5, 15), 5) for i in range(6)],
        )
    build_phenology(con, CELL)
    _add_fire(
        con,
        id="onfar",
        center_lat=far_lat,
        center_lng=far_lng,
        min_lat=far_lat - 0.1,
        max_lat=far_lat + 0.1,
        min_lng=far_lng - 0.1,
        max_lng=far_lng + 0.1,
    )
    ranked = rank_destinations(
        con, months=[5], taxon_ids=[MORCHELLA], home_lat=far_lat, home_lng=far_lng, radius_km=300, cell_deg=CELL
    )
    far_region = next(r for r in ranked if abs(r.center_lat - far_lat) < CELL)
    assert far_region.fire_nearby and far_region.fire_nearby[0].distance_km < 10


def test_active_fire_penalises_the_region(con: psycopg.Connection) -> None:
    baseline = _rank(con, [MORCHELLA, OTHER])[0].score
    _add_fire(con)
    after = _rank(con, [MORCHELLA, OTHER])
    assert after[0].score < baseline
    assert after[0].fire_nearby and after[0].fire_nearby[0].status == "active"


def test_burn_scar_boosts_only_when_morchella_targeted(con: psycopg.Connection) -> None:
    _add_fire(con, id="scar", source_key="perimeter_history", status="historical", fire_year=THIS_YEAR - 1)
    with_morchella = _rank(con, [MORCHELLA])[0].score
    without = _rank(con, [OTHER])[0].score
    # Same phenology base (both genera fruit here equally in the seed), so the boost is the only
    # difference - Morchella-targeted score is higher.
    assert with_morchella > without


def test_high_severity_scar_is_not_boosted(con: psycopg.Connection) -> None:
    base = _rank(con, [MORCHELLA])[0].score
    _add_fire(
        con,
        id="scar",
        source_key="perimeter_history",
        status="historical",
        fire_year=THIS_YEAR - 1,
        dominant_severity="high",
    )
    assert _rank(con, [MORCHELLA])[0].score == base  # high severity -> no morel boost
