"""Region ranking, the point-and-radius read repository, and the trip planner (issue #242).

Submodules:

* ``foray.scoring.models`` - the result dataclasses
* ``foray.scoring.regions`` - ``build_phenology`` + the materialized-table helpers
* ``foray.scoring.ranking`` - the region-ranking modes
* ``foray.scoring.queries`` - the point-and-radius read repository
* ``foray.scoring.planner`` - ``plan_route``
* ``foray.scoring._sql`` - shared SQL fragment builders

(Pure lat/lng math is ``foray.geo``, a package-level primitive shared with ``cache``/``camps``/
``land``.) This ``__init__`` re-exports the public surface so ``from foray.scoring import
rank_destinations`` and ``foray.scoring.<name>`` keep working from call sites and tests.
"""

from __future__ import annotations

from foray.scoring.models import (
    CampSite,
    LandUnit,
    RegionScore,
    SpeciesHit,
    Stop,
    Trail,
    TrailPath,
    TripPlan,
)
from foray.scoring.planner import plan_route
from foray.scoring.queries import (
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
from foray.scoring.ranking import rank_destinations, rank_destinations_corridor
from foray.scoring.regions import build_phenology

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
