"""Back-compat re-export shim for the old monolithic ``scoring`` module.

The scoring code now lives in a small package (issue #242):

* ``foray.geo`` - pure lat/lng math
* ``foray.models`` - the result dataclasses
* ``foray.regions`` - ``build_phenology`` + the materialized-table helpers
* ``foray.ranking`` - the region-ranking modes
* ``foray.queries`` - the point-and-radius read repository
* ``foray.planner`` - ``plan_route``

Import from those directly in new code. This shim keeps ``foray.scoring.<name>`` working for
one release; it will be removed.
"""

from __future__ import annotations

from foray.models import (
    CampSite,
    LandUnit,
    RegionScore,
    SpeciesHit,
    Stop,
    Trail,
    TrailPath,
    TripPlan,
)
from foray.planner import plan_route
from foray.queries import (
    alerts,
    camps_near,
    get_trail,
    land_near,
    nearest_trail,
    place_calendar,
    precise_observations,
    recent_observations,
    trails_near,
)
from foray.ranking import rank_destinations, rank_destinations_corridor
from foray.regions import build_phenology

__all__ = [
    "CampSite",
    "LandUnit",
    "RegionScore",
    "SpeciesHit",
    "Stop",
    "Trail",
    "TrailPath",
    "TripPlan",
    "alerts",
    "build_phenology",
    "camps_near",
    "get_trail",
    "land_near",
    "nearest_trail",
    "place_calendar",
    "plan_route",
    "precise_observations",
    "rank_destinations",
    "rank_destinations_corridor",
    "recent_observations",
    "trails_near",
]
