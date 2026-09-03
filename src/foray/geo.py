"""Geo primitives shared across scoring, the read repository, and the ingest layer.

Everything here is pure math on lat/lng - no I/O, no DB. ``haversine_km`` is the canonical
great-circle distance used everywhere an exact distance matters (region math, corridor
planning, alerts). ``bbox_around`` builds a cheap flat-degree bounding box - since PostGIS
Phase 1 (issue #268) the ``*_near`` reads filter on the ``geom`` GIST index, so its only
callers now are the ingest sources building an ArcGIS/Overpass fetch envelope.
"""

from __future__ import annotations

import math
from typing import NamedTuple

# Degrees of latitude per kilometre is very nearly constant (~111 km/deg); longitude is scaled
# by cos(lat) at the point of interest. This is the flat-degree approximation the bbox
# prefilters and the corridor tangent-plane projection both rely on - fine at the scales this
# app works over (tens to low hundreds of km), and deliberately coarse.
KM_PER_DEG_LAT = 111.0


class BBox(NamedTuple):
    """A lat/lng bounding box. Field order is (min_lat, min_lng, max_lat, max_lng)."""

    min_lat: float
    min_lng: float
    max_lat: float
    max_lng: float


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    earth_radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    inner = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return 2 * earth_radius_km * math.asin(math.sqrt(inner))


def _lng_km_per_deg(lat: float) -> float:
    """Kilometres per degree of longitude at ``lat`` (floored so it never blows up near a pole)."""
    return KM_PER_DEG_LAT * max(abs(math.cos(math.radians(lat))), 0.01)


def bbox_around(lat: float, lng: float, radius_km: float) -> BBox:
    """Flat-degree bounding box of the disk of ``radius_km`` around ``(lat, lng)``.

    ``radius_km / 111`` / ``radius_km / (111 * cos lat)``. Coarse on purpose - the ingest
    sources only need a superset of the true disk to hand an ArcGIS / Overpass query as its
    spatial envelope.
    """
    dlat = radius_km / KM_PER_DEG_LAT
    dlng = radius_km / _lng_km_per_deg(lat)
    return BBox(lat - dlat, lng - dlng, lat + dlat, lng + dlng)


def bbox_around_segment(lat1: float, lng1: float, lat2: float, lng2: float, radius_km: float) -> BBox:
    """Flat-degree bounding box covering ``radius_km`` around the segment ``1 -> 2``.

    The corridor analogue of :func:`bbox_around`: a superset of every point within
    ``radius_km`` of the straight line between the two endpoints, used to turn a plan
    corridor into a ``region_id`` allowlist for the phenology ranking query.
    """
    dlat = radius_km / KM_PER_DEG_LAT
    dlng = radius_km / _lng_km_per_deg(max(abs(lat1), abs(lat2)))
    return BBox(
        min(lat1, lat2) - dlat,
        min(lng1, lng2) - dlng,
        max(lat1, lat2) + dlat,
        max(lng1, lng2) + dlng,
    )


class GridCell(NamedTuple):
    """A grid cell on the same ``floor(coord / cell_deg)`` lattice ``scoring._sql.BINNED``
    derives ``region_id`` from. ``cell_id`` matches that ``region_id`` exactly."""

    cell_id: str
    center_lat: float
    center_lng: float


def grid_cell(lat: float, lng: float, cell_deg: float) -> GridCell:
    """Snap ``(lat, lng)`` to its grid cell - the same ``"{ilat}_{ilng}"`` key
    ``regions``/``phenology`` compute in SQL, plus the cell's center point.

    Used by the precip cache (issue #226) to reuse the region grid as the weather geography
    instead of hitting Open-Meteo per raw observation coordinate.
    """
    ilat = math.floor(lat / cell_deg)
    ilng = math.floor(lng / cell_deg)
    return GridCell(f"{ilat}_{ilng}", (ilat + 0.5) * cell_deg, (ilng + 0.5) * cell_deg)


def grid_cells_in_bbox(bbox: BBox, cell_deg: float) -> list[str]:
    """Every ``"{ilat}_{ilng}"`` cell id whose cell intersects ``bbox``.

    Same ``floor(coord / cell_deg)`` lattice as :func:`grid_cell`. Lets the ranking path
    turn a home-radius / corridor envelope into an explicit ``region_id`` allowlist so the
    phenology query hits ``ix_phenology_region`` instead of aggregating every ingested cell
    on the planet and discarding the out-of-range ones in Python.
    """
    ilat_lo = math.floor(bbox.min_lat / cell_deg)
    ilat_hi = math.floor(bbox.max_lat / cell_deg)
    ilng_lo = math.floor(bbox.min_lng / cell_deg)
    ilng_hi = math.floor(bbox.max_lng / cell_deg)
    return [f"{ilat}_{ilng}" for ilat in range(ilat_lo, ilat_hi + 1) for ilng in range(ilng_lo, ilng_hi + 1)]


def grid_cell_center(cell_id: str, cell_deg: float) -> tuple[float, float]:
    """Inverse of :func:`grid_cell` for the center point: ``"{ilat}_{ilng}"`` -> ``(lat, lng)``."""
    ilat_str, ilng_str = cell_id.split("_")
    return (int(ilat_str) + 0.5) * cell_deg, (int(ilng_str) + 0.5) * cell_deg


def project_to_plane(ref_lat: float, ref_lng: float, lat: float, lng: float) -> tuple[float, float]:
    """Local tangent-plane projection (x=east km, y=north km) centered on ``ref_lat``/``ref_lng``.

    Same flat-degree approximation the bbox prefilters use, applied once per point instead of
    per-bbox-edge. Fine at corridor scale (tens to low hundreds of km); ``ref_lat`` is fixed
    for every point in a given call so the longitude scale distortion is consistent, not
    per-point re-biased.
    """
    y = (lat - ref_lat) * KM_PER_DEG_LAT
    x = (lng - ref_lng) * _lng_km_per_deg(ref_lat)
    return x, y


def segment_progress_and_offset(px: float, py: float, dx: float, dy: float) -> tuple[float, float]:
    """Project point ``(px, py)`` onto the segment from the origin to ``(dx, dy)``.

    Returns ``(t_clamped, offset_km)``: ``t_clamped`` is the projection parameter clamped to
    ``[0, 1]`` (so a point beyond either end measures its offset to that endpoint, not the
    infinite line - the correct corridor semantics), and ``offset_km`` is the perpendicular
    distance from the point to the clamped projection, i.e. the corridor-width test.
    """
    seg_len2 = dx * dx + dy * dy
    if seg_len2 == 0:
        return 0.0, math.hypot(px, py)
    t = (px * dx + py * dy) / seg_len2
    t_clamped = max(0.0, min(1.0, t))
    proj_x, proj_y = t_clamped * dx, t_clamped * dy
    offset_km = math.hypot(px - proj_x, py - proj_y)
    return t_clamped, offset_km
