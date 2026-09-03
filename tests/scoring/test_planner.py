"""Trip-planner tests on hand-built fixtures - no network (per python skill: hermetic)."""

from __future__ import annotations

import datetime as dt
import json
import math

import psycopg
import pytest

from foray.cache import upsert_campsites, upsert_trails
from foray.scoring import build_phenology, plan_route

CELL = 0.5
MOREL = 111

# Start near (44.0, -121.0). NEAR/MID/FAR sit due north of start, on the same meridian, all
# active in October with decreasing observation counts (near = strongest score) so ordering is
# deterministic. DEST is further north still, past FAR, so NEAR/MID/FAR are all on the
# start->DEST corridor by construction. OFF_CORRIDOR sits at MID's latitude but well east of the
# meridian, so it's roughly as far from start as MID but well outside the default corridor width -
# the key regression a purely radial fixture can't express.
START_LAT, START_LNG = 44.0, -121.0
NEAR = (44.2, -121.0)  # ~22 km N of start
MID = (45.0, -121.0)  # ~111 km N of start
FAR = (47.0, -121.0)  # ~333 km N of start
DEST = (48.0, -121.0)  # ~444 km N of start
OFF_CORRIDOR = (45.0, -119.87)  # ~same latitude as MID, ~90 km E of the corridor line


def _region_id(lat: float, lng: float, cell: float = CELL) -> str:
    """Mirror scoring.py's ``_BINNED`` grid-binning exactly (``floor``, not ``int``) - matters for
    negative, non-multiple-of-``cell`` coordinates like OFF_CORRIDOR's longitude, where ``int()``
    truncates toward zero and disagrees with ``floor()``."""
    return f"{math.floor(lat / cell)}_{math.floor(lng / cell)}"


@pytest.fixture(autouse=True)
def _seed(con: psycopg.Connection) -> None:
    con.execute(
        "INSERT INTO fungi_genera (taxon_id, name, common_name) VALUES (%s, %s, %s)", (MOREL, "Morchella", "Morels")
    )

    rows: list[tuple] = []
    obs_id = 1
    # More observations closer in (and OFF_CORRIDOR deliberately scored higher than MID, so a
    # broken corridor filter that let it through would be obvious in ordering assertions).
    for (lat, lng), obs_count in ((NEAR, 40), (MID, 25), (FAR, 15), (OFF_CORRIDOR, 30)):
        for _ in range(obs_count):
            rows.append((obs_id, MOREL, lat, lng, dt.date(2022, 10, 15), 10, "research", 10))
            obs_id += 1
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month,"
            " quality_grade, positional_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
    build_phenology(con, CELL)

    # A free camp beside NEAR and MID; FAR gets only a paid camp; OFF_CORRIDOR gets a free camp
    # too (so it would definitely pass stop selection if the corridor filter didn't exclude it).
    upsert_campsites(
        con,
        [
            ("osm:1", "Free NEAR", "dispersed", None, True, NEAR[0], NEAR[1], "osm", "u"),
            ("osm:2", "Free MID", "dispersed", None, True, MID[0], MID[1], "osm", "u"),
            ("ridb:3", "Paid FAR", "campground", "$20", None, FAR[0], FAR[1], "ridb", "u"),
            ("osm:4", "Free OFF_CORRIDOR", "dispersed", None, True, OFF_CORRIDOR[0], OFF_CORRIDOR[1], "osm", "u"),
        ],
    )

    # A trail right at NEAR, so Stop.trail can be asserted on.
    point = json.dumps({"type": "Point", "coordinates": [NEAR[1], NEAR[0]]})
    upsert_trails(
        con,
        [
            ("osm:trail:1", "Near Loop Trail", "trail", "osm", "u", NEAR[0], NEAR[1], point),
        ],
    )


def _kwargs(**overrides: object) -> dict:
    base: dict = {
        "months": [10],
        "taxon_ids": [MOREL],
        "cell_deg": CELL,
        "start_lat": START_LAT,
        "start_lng": START_LNG,
        "destination_lat": DEST[0],
        "destination_lng": DEST[1],
    }
    base.update(overrides)
    return base


def test_plan_orders_stops_by_progress_along_corridor(con: psycopg.Connection) -> None:
    trip = plan_route(con, **_kwargs(require_free_camp=False))
    # NEAR, MID, FAR are all on the corridor and on the way to DEST; OFF_CORRIDOR is excluded
    # even though it out-scores MID and FAR.
    assert [s.order for s in trip.stops] == [1, 2, 3]
    assert [round(s.center_lat, 1) for s in trip.stops] == [44.2, 45.0, 47.0]
    assert trip.stops[0].progress_km < trip.stops[1].progress_km < trip.stops[2].progress_km
    assert trip.stops[-1].cumulative_drive_km == trip.total_drive_km
    assert trip.n_stops == 3
    assert trip.destination_name is None  # explicit destination, not auto-picked
    assert trip.auto_destination is False


def test_corridor_excludes_off_axis_region(con: psycopg.Connection) -> None:
    trip = plan_route(con, **_kwargs(require_free_camp=False))
    off_region = _region_id(*OFF_CORRIDOR)
    assert off_region not in {s.region_id for s in trip.stops}


def test_stop_includes_nearest_trail(con: psycopg.Connection) -> None:
    trip = plan_route(con, **_kwargs(require_free_camp=False))
    near = next(s for s in trip.stops if round(s.center_lat, 1) == 44.2)
    assert near.trail is not None
    assert near.trail.name == "Near Loop Trail"
    assert near.trail_distance_km == near.trail.distance_km


def test_require_free_camp_drops_paid_only_stops(con: psycopg.Connection) -> None:
    trip = plan_route(con, **_kwargs(require_free_camp=True))
    ids = {s.region_id for s in trip.stops}
    # FAR has only a paid camp -> excluded; NEAR and MID keep their free camp.
    assert len(trip.stops) == 2
    assert all(s.camp_is_free and s.camp is not None for s in trip.stops)
    far_region = _region_id(*FAR)
    assert far_region not in ids


def test_any_camp_annotates_nearest_paid_camp(con: psycopg.Connection) -> None:
    trip = plan_route(con, **_kwargs(require_free_camp=False))
    far = next(s for s in trip.stops if s.camp is not None and not s.camp_is_free)
    assert far.camp is not None and far.camp.name == "Paid FAR"


def test_max_stops_caps_the_itinerary(con: psycopg.Connection) -> None:
    trip = plan_route(con, **_kwargs(require_free_camp=False, max_stops=2))
    assert trip.n_stops == 2
    # The two highest-scoring on-corridor regions (NEAR, MID) are kept, not FAR.
    assert {s.region_id for s in trip.stops} == {_region_id(*NEAR), _region_id(*MID)}


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


def test_auto_pick_chooses_best_scoring_region_beyond_min_distance(con: psycopg.Connection) -> None:
    # radius (130 km) excludes OFF_CORRIDOR (~143 km, despite scoring highest) and FAR (~333
    # km); min distance excludes NEAR (~22 km) - MID (~111 km) is the sole survivor and becomes
    # the auto-picked destination.
    trip = plan_route(
        con,
        **_kwargs(
            destination_lat=None,
            destination_lng=None,
            require_free_camp=False,
            auto_pick_radius_km=130,
            auto_pick_min_km=50,
        ),
    )
    assert trip.auto_destination is True
    assert trip.destination_name == _region_id(*MID)
    assert round(trip.destination_lat, 1) == 45.0
    # NEAR sits on the start->MID corridor too, so it's still a stop along the way.
    assert {s.region_id for s in trip.stops} >= {_region_id(*NEAR)}


def test_auto_pick_empty_when_nothing_clears_minimum_distance(con: psycopg.Connection) -> None:
    trip = plan_route(
        con,
        **_kwargs(destination_lat=None, destination_lng=None, auto_pick_radius_km=10, auto_pick_min_km=5),
    )
    assert trip.auto_destination is True
    assert trip.destination_name is None
    assert trip.n_stops == 0
    assert trip.stops == []


def test_degenerate_destination_does_not_crash(con: psycopg.Connection) -> None:
    # Destination equal to start collapses the corridor to a disk around start; should still
    # run cleanly (falls back to radial distance for the offset test) rather than raising.
    trip = plan_route(
        con,
        **_kwargs(destination_lat=START_LAT, destination_lng=START_LNG, require_free_camp=False),
    )
    assert trip.auto_destination is False
    # Only NEAR (~22 km) is within the default 60 km corridor radius of a collapsed segment.
    assert {s.region_id for s in trip.stops} == {_region_id(*NEAR)}
    # Regression: a degenerate segment must fall back to radial distance as progress, not a flat
    # 0 for every candidate (which would collapse the sort order for any case with >1 stop).
    assert trip.stops[0].progress_km > 0


# DETOUR sits well east of the corridor line at a middling latitude - close enough to survive the
# default 60 km corridor width, but far enough from NEAR that the direct NEAR->DETOUR leg exceeds
# a deliberately tight max_drive_km. NEXT sits back on the line, further along in progress than
# DETOUR but much closer to NEAR in real distance - the exact "off-axis zigzag" the docstring
# warns about. Only inserted for this test (not the shared fixture) so it can't perturb the
# progress/ordering assertions of the other corridor tests above.
DETOUR = (44.8, -120.303)  # ~89 km progress, ~86 km real distance from NEAR
NEXT = (44.95, -121.0)  # ~106 km progress, ~83 km real distance from NEAR


def test_unreachable_stop_is_skipped_not_truncating_the_rest(con: psycopg.Connection) -> None:
    rows = []
    obs_id = 10_000
    for lat, lng in (DETOUR, NEXT):
        for _ in range(20):
            rows.append((obs_id, MOREL, lat, lng, dt.date(2022, 10, 15), 10, "research", 10))
            obs_id += 1
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO observations (id, taxon_id, lat, lng, observed_on, month,"
            " quality_grade, positional_accuracy) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
    build_phenology(con, CELL)

    # 85 km reaches NEAR->NEXT (~83) and NEXT->MID (~6) but not NEAR->DETOUR (~86) or MID->FAR
    # (~222) - DETOUR and FAR should each be individually skipped, not cascade into dropping
    # NEXT/MID too.
    trip = plan_route(con, **_kwargs(require_free_camp=False, max_drive_km=85, max_stops=20))
    assert [round(s.center_lat, 2) for s in trip.stops] == [44.2, 44.95, 45.0]
    assert trip.skipped_unreachable == 2
