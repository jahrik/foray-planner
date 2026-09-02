"""Plain stdlib dataclasses returned by the scoring package.

These are the shapes the ``ranking`` / ``queries`` / ``planner`` modules produce and that
``api_models.py`` mirrors as Pydantic response models (``from_attributes=True``). Kept in
their own module - no DB or HTTP imports - so any layer can depend on them without pulling in
query code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SpeciesHit:
    taxon_id: int
    name: str
    common_name: str | None
    month_count: int
    total_count: int
    w_pheno: float


@dataclass
class RegionScore:
    region_id: str
    center_lat: float
    center_lng: float
    distance_km: float
    score: float
    score_norm: float
    n_species: int
    recent_count: int
    species: list[SpeciesHit]
    elevation_m: int | None = None
    # Antecedent-rainfall means over the region's enriched observations (issue #226), mm.
    precip_obs_7d_mm: float | None = None
    precip_obs_30d_mm: float | None = None
    # Recent rainfall at the region cell right now, from the precipitation layer (issue #226
    # Part 2), mm over the trailing 7 / 14 / 30 days. None until the layer has been refreshed.
    precip_recent_7d_mm: float | None = None
    precip_recent_14d_mm: float | None = None
    precip_recent_30d_mm: float | None = None
    # Nearby active fires (warnings) and recent burn scars (morel opportunities), issue #227.
    fire_nearby: list[FireNear] = field(default_factory=list)


@dataclass
class CampSite:
    id: str
    name: str
    kind: str
    fee: str | None
    free: bool | None
    center_lat: float
    center_lng: float
    distance_km: float
    source: str
    url: str


@dataclass
class FireNear:
    """An active wildfire or recent burn scar near a point (issue #227). ``geometry`` is only
    populated for the map layer (`GET /api/fire`); the card/scoring paths leave it None."""

    id: str
    name: str
    status: str  # 'active' | 'historical'
    fire_year: int | None
    center_lat: float
    center_lng: float
    distance_km: float
    percent_contained: float | None
    gis_acres: float | None
    dominant_severity: str | None  # 'low' | 'moderate' | 'high' | None
    is_point: bool
    incident_url: str | None
    geometry: dict[str, Any] | None = None


@dataclass
class LandUnit:
    id: str
    agency: str
    unit: str
    source: str
    url: str
    geometry: dict[str, Any]  # parsed GeoJSON geometry, ready for Leaflet


@dataclass
class Trail:
    id: str
    name: str
    kind: str
    source: str
    url: str
    center_lat: float
    center_lng: float
    distance_km: float  # from the hotspot to the trail's representative point
    camp_distance_km: float | None  # nearest cached campsite to the trail ("park → hike → fungi")
    geometry: dict[str, Any]  # parsed GeoJSON geometry, ready for Leaflet


@dataclass
class TrailPath:
    """A ``Trail`` plus whether its geometry came from a real OSM link or a proximity guess.

    See ``trails.resolve_trail_network``: ``authoritative=True`` means the trailhead node shares
    OSM topology (way/route membership) with the returned geometry; ``False`` means it's the
    nearest cached path/route found by ``queries.nearest_trail`` instead - a heuristic the UI
    should show distinctly (see AGENTS.md, "No claims").
    """

    trail: Trail
    authoritative: bool


@dataclass
class Stop:
    """One week-long stay in a planned trip: a destination + how you get there + where you sleep."""

    order: int  # 1-based position in the itinerary
    region_id: str
    center_lat: float
    center_lng: float
    score_norm: float  # destination score relative to the best region (0..1)
    n_species: int
    recent_count: int
    species: list[SpeciesHit]
    progress_km: float  # distance along the start->destination chord (0 at start)
    drive_km_from_prev: float  # great-circle leg from the previous stop (or start for stop 1)
    cumulative_drive_km: float  # running total from start, actual point-to-point (not progress_km)
    camp: CampSite | None  # closest free camp (or closest of any kind if none is free-tagged)
    camp_is_free: bool
    trail: Trail | None  # closest trail in range, if any
    trail_distance_km: float | None
    fire_nearby: list[FireNear] = field(default_factory=list)  # active-fire warnings on this stop (issue #227)


@dataclass
class TripPlan:
    start_lat: float
    start_lng: float
    destination_lat: float
    destination_lng: float
    destination_name: str | None  # region_id when auto-picked; None for a caller-supplied destination
    auto_destination: bool
    corridor_km: float
    months: list[int]
    n_stops: int
    total_drive_km: float
    stops: list[Stop]
    skipped_unreachable: int  # viable candidates dropped for being past ``max_drive_km`` from route
