"""Pydantic response models for the JSON API (src/foray/api.py).

These formalize the shapes already produced by ``foray.scoring``'s stdlib dataclasses and a
few endpoint-only envelopes, so FastAPI's generated OpenAPI schema carries real field types
for *responses*, not just request bodies/params. Field names mirror the dataclasses exactly
(``scoring.SpeciesHit``, ``RegionScore``, ``CampSite``, ``LandUnit``, ``Trail``, ``Stop``,
``TripPlan``) - no new shapes invented, just typed reflections of what the routes already
return.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from foray.config import Home

# Several models below mirror the stdlib dataclasses in foray.scoring (SpeciesHit, RegionScore,
# CampSite, LandUnit, Trail, Stop, TripPlan). Routes return those dataclass instances directly
# (see api.py), so those models need `from_attributes=True` to validate them; it's harmless for
# the dict-shaped inputs the other models validate (alerts/observations/calendar all build plain
# dicts in foray.scoring).
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
    drive_km_from_prev: float
    cumulative_drive_km: float
    camp: CampSite | None
    camp_is_free: bool


class TripPlan(BaseModel):
    model_config = _FROM_DATACLASS

    home_lat: float
    home_lng: float
    months: list[int]
    n_stops: int
    total_drive_km: float
    stops: list[Stop]
    skipped_unreachable: int


class LocationResponse(BaseModel):
    home: Home


class StatusResponse(BaseModel):
    status: str
