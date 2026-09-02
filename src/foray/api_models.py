"""Pydantic response models for the JSON API (src/foray/api/).

These formalize the shapes already produced by ``foray.scoring.models``'s stdlib dataclasses and a
few endpoint-only envelopes, so FastAPI's generated OpenAPI schema carries real field types
for *responses*, not just request bodies/params. Field names mirror the dataclasses exactly
(``models.SpeciesHit``, ``RegionScore``, ``CampSite``, ``LandUnit``, ``Trail``, ``Stop``,
``TripPlan``) - no new shapes invented, just typed reflections of what the routes already
return.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from foray.config import Home

# Several models below mirror the stdlib dataclasses in foray.scoring.models (SpeciesHit, RegionScore,
# CampSite, LandUnit, Trail, Stop, TripPlan). Routes return those dataclass instances directly
# (see api/routes/), so those models need `from_attributes=True` to validate them; it's harmless for
# the dict-shaped inputs the other models validate (alerts/observations/calendar all build plain
# dicts in foray.scoring.queries).
_FROM_DATACLASS = ConfigDict(from_attributes=True)


class ConfigResponse(BaseModel):
    home: Home
    cell_deg: float
    recent_weeks: int
    refreshing: bool
    last_error: str | None


class GenusResult(BaseModel):
    """A genus catalog search hit (issue #79).

    ``common_name`` is optional - most of the ~6,018 Fungi genera lack an English common name
    on iNat, so ``name`` (scientific) is the primary label, not a fallback.
    """

    taxon_id: int
    name: str
    common_name: str | None


class CoverageRegionResponse(BaseModel):
    name: str
    place_id: int
    last_ingest: str | None
    observations_ingested: int


class SpeciesHit(BaseModel):
    """A target-genus contribution to a ranked region.

    ``name`` (scientific) is the primary display label; ``common_name`` is optional
    secondary enrichment - most of the ~6,018-genus catalog lacks an English common name.
    """

    model_config = _FROM_DATACLASS

    taxon_id: int
    name: str
    common_name: str | None
    month_count: int
    total_count: int
    w_pheno: float


class FireNear(BaseModel):
    """An active wildfire or recent burn scar near a point (issue #227). ``geometry`` is only
    populated for the map layer (`GET /api/fire`)."""

    model_config = _FROM_DATACLASS

    id: str
    name: str
    status: str
    fire_year: int | None
    center_lat: float
    center_lng: float
    distance_km: float
    percent_contained: float | None
    gis_acres: float | None
    dominant_severity: str | None
    is_point: bool
    incident_url: str | None
    geometry: dict[str, Any] | None = None


class RegionScore(BaseModel):
    model_config = _FROM_DATACLASS

    region_id: str
    center_lat: float
    center_lng: float
    distance_km: float
    score: float
    score_norm: float
    n_species: int
    recent_count: int
    species: list[SpeciesHit]
    # Mean ground elevation of the region's observations, metres (issue #36). None until at
    # least one observation in the region has been elevation-enriched.
    elevation_m: int | None = None
    # Antecedent rainfall (issue #226): decoy-excluded mean over the region's precip-enriched
    # observations (mm over the 7 / 30 days before each was observed). None until enriched.
    precip_obs_7d_mm: float | None = None
    precip_obs_30d_mm: float | None = None
    # Recent rainfall at the region cell now (issue #226 Part 2), mm over the trailing 7 / 14 /
    # 30 days. None until the precipitation layer has been refreshed.
    precip_recent_7d_mm: float | None = None
    precip_recent_14d_mm: float | None = None
    precip_recent_30d_mm: float | None = None
    # Nearby active fires (warnings) + recent burn scars (morel opportunities), issue #227.
    fire_nearby: list[FireNear] = []


class RegionPlace(BaseModel):
    """A destination card's titling place name (issue #206), or ``None`` when the region has
    no notable place nearby."""

    place_name: str | None


class CalendarBucket(BaseModel):
    total: int
    species: dict[str, int]


class ObservationPhoto(BaseModel):
    url: str
    license_code: str
    attribution: str


class RecentObservation(BaseModel):
    id: int
    taxon_id: int
    name: str
    common_name: str | None
    observed_on: str | None
    place_guess: str | None
    uri: str | None
    obscured: bool
    photos: list[ObservationPhoto]


class RecentObservationsPage(BaseModel):
    observations: list[RecentObservation]
    has_more: bool


class PreciseObservation(BaseModel):
    id: int
    taxon_id: int
    name: str
    common_name: str | None
    lat: float
    lng: float
    observed_on: str | None
    uri: str | None


class AlertHit(BaseModel):
    taxon_id: int
    name: str
    common_name: str | None
    count: int
    last_seen: str
    place_guess: str | None
    uri: str | None
    obscured: bool


class AlertRegion(BaseModel):
    region_id: str
    center_lat: float
    center_lng: float
    distance_km: float
    total: int
    species: list[AlertHit]
    # Recent rainfall at the region cell (issue #226 Part 2), mm over the trailing 7 / 14 / 30
    # days - the road-trip "go now" signal. None until the precipitation layer has refreshed.
    precip_recent_7d_mm: float | None = None
    precip_recent_14d_mm: float | None = None
    precip_recent_30d_mm: float | None = None
    # Nearby active fires + recent burn scars (issue #227).
    fire_nearby: list[FireNear] = []


class CampSite(BaseModel):
    model_config = _FROM_DATACLASS

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


class LandUnit(BaseModel):
    model_config = _FROM_DATACLASS

    id: str
    agency: str
    unit: str
    source: str
    url: str
    geometry: dict[str, Any]  # raw GeoJSON geometry - not modeled further, see api_models docstring


class Trail(BaseModel):
    model_config = _FROM_DATACLASS

    id: str
    name: str
    kind: str
    source: str
    url: str
    center_lat: float
    center_lng: float
    distance_km: float
    camp_distance_km: float | None
    geometry: dict[str, Any]  # raw GeoJSON geometry, same as LandUnit.geometry


class TrailPath(BaseModel):
    model_config = _FROM_DATACLASS

    trail: Trail
    authoritative: bool


class Stop(BaseModel):
    model_config = _FROM_DATACLASS

    order: int
    region_id: str
    center_lat: float
    center_lng: float
    score_norm: float
    n_species: int
    recent_count: int
    species: list[SpeciesHit]
    progress_km: float
    drive_km_from_prev: float
    cumulative_drive_km: float
    camp: CampSite | None
    camp_is_free: bool
    trail: Trail | None
    trail_distance_km: float | None
    fire_nearby: list[FireNear] = []


class TripPlan(BaseModel):
    model_config = _FROM_DATACLASS

    start_lat: float
    start_lng: float
    destination_lat: float
    destination_lng: float
    destination_name: str | None
    auto_destination: bool
    corridor_km: float
    months: list[int]
    n_stops: int
    total_drive_km: float
    stops: list[Stop]
    skipped_unreachable: int


class PlaceSuggestion(BaseModel):
    """One typeahead hit from ``/api/location/search`` (issue #145)."""

    name: str
    lat: float
    lng: float


class LocationResponse(BaseModel):
    home: Home


class StatusResponse(BaseModel):
    status: str
