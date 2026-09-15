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

import h3

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


# Equatorial radius used by the standard "spherical" Web Mercator (EPSG:3857) that every slippy
# tile / ArcGIS service - and Leaflet's default CRS - projects through. Matches Leaflet's
# L.CRS.EPSG3857 exactly (same R): the frontend's destination circle (an L.circle with a meter
# radius) and the backend's stitched satellite raster (sources.satellite) must agree on this
# projection, or the fetched image mismatches what Leaflet stretches it into.
_WEB_MERCATOR_R = 6378137.0

# EPSG:3857 is undefined past this latitude (the projection formula below diverges to +-infinity
# at the poles) - this is the same clamp Leaflet's own CRS.EPSG3857 applies, so a region whose
# grid cell happens to straddle it still gets a finite, Leaflet-consistent bbox instead of a
# ValueError from math.log(tan(...)) going to zero/infinity.
_WEB_MERCATOR_MAX_LAT = 85.0511287798


def web_mercator_bbox_m(lat: float, lng: float, radius_m: float) -> tuple[float, float, float, float]:
    """Bounding square, in EPSG:3857 meters, of the disk of ``radius_m`` around ``(lat, lng)``.

    ``(xmin, ymin, xmax, ymax)`` - center projected via the standard spherical Web Mercator
    formula, then offset on each axis by ``radius_m`` scaled up by the Web Mercator point scale
    ``1 / cos(lat)``. That scale factor matters: ``radius_m`` is a real-world ground distance, but
    an EPSG:3857 offset is in *projected* meters, and the projection stretches by ``sec(lat)``
    away from the equator (~1.48x at Puget Sound). Leaflet's ``L.circle`` is a geodesic circle,
    so the lat/lng box it reports from ``getBounds()`` - the box the frontend drops the imagery
    into - spans ``±radius_m / cos(lat)`` in projected meters, not ``±radius_m``. Matching that
    here is what keeps the satellite raster aligned with the circle (and the basemap under it)
    instead of stretched ~48% too large. Used by ``sources.satellite``.
    """
    clamped_lat = max(-_WEB_MERCATOR_MAX_LAT, min(_WEB_MERCATOR_MAX_LAT, lat))
    x = math.radians(lng) * _WEB_MERCATOR_R
    y = math.log(math.tan(math.pi / 4 + math.radians(clamped_lat) / 2)) * _WEB_MERCATOR_R
    offset = radius_m / math.cos(math.radians(clamped_lat))
    return (x - offset, y - offset, x + offset, y + offset)


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


def h3_edge_length_km(h3_resolution: int) -> float:
    """Average H3 cell edge length at ``h3_resolution``, in km (issue #337).

    A per-resolution constant, not a per-cell one - every cell at a given resolution is close
    enough to the same size (H3's whole point) that the average is precise enough for the
    "pad by about one cell width" uses this has (``planner``'s corridor widening,
    ``satellite.backfill_region_satellite``'s fetch radius). Matches the SQL-side
    ``h3_get_hexagon_edge_length_avg(resolution, 'km')`` exactly - same core H3 library.
    """
    return h3.average_hexagon_edge_length(h3_resolution, unit="km")


class GridCell(NamedTuple):
    """An H3 cell (issue #337) on the same lattice ``scoring._sql.BINNED`` derives
    ``region_id`` from in SQL. ``cell_id`` matches that ``region_id`` exactly - both are the
    cell's H3 index in its canonical hex-string form."""

    cell_id: str
    center_lat: float
    center_lng: float


def grid_cell(lat: float, lng: float, h3_resolution: int) -> GridCell:
    """Snap ``(lat, lng)`` to its H3 cell - the same key ``regions``/``phenology`` compute in
    SQL (``h3_lat_lng_to_cell``), plus the cell's center point.

    Used by the precip cache (issue #226) to reuse the region grid as the weather geography
    instead of hitting Open-Meteo per raw observation coordinate. Backed by the ``h3`` package
    (bindings for the same core H3 C library the Postgres ``h3`` extension wraps), not a
    Postgres round trip - cell assignment is a pure function of ``(lat, lng, resolution)``, so
    both sides always agree without either one calling the other.
    """
    cell_id = h3.latlng_to_cell(lat, lng, h3_resolution)
    center_lat, center_lng = h3.cell_to_latlng(cell_id)
    return GridCell(cell_id, center_lat, center_lng)


def bbox_center_radius(bbox: BBox) -> tuple[float, float, float]:
    """A circle that contains ``bbox``: its midpoint as the centre, and the great-circle
    distance to the farthest corner as the radius.

    Not the global minimum enclosing circle, just a compact one - enough to turn a
    candidate-area bounding box into the ``(centre, radius)`` an index-backed ``ST_DWithin``
    needs without over-fetching the way a start-anchored radius would. The box's corners are
    its farthest points from the midpoint, so covering them covers the whole box.
    """
    center_lat = (bbox.min_lat + bbox.max_lat) / 2
    center_lng = (bbox.min_lng + bbox.max_lng) / 2
    # Lines of longitude converge, so the box isn't symmetric under great-circle distance -
    # the farthest corner is one of the two low-latitude ones, not simply (max_lat, max_lng).
    radius_km = max(
        haversine_km(center_lat, center_lng, corner_lat, corner_lng)
        for corner_lat in (bbox.min_lat, bbox.max_lat)
        for corner_lng in (bbox.min_lng, bbox.max_lng)
    )
    return center_lat, center_lng, radius_km


def cells_in_radius(lat: float, lng: float, radius_km: float, h3_resolution: int) -> list[str]:
    """Every H3 cell within ``radius_km`` of ``(lat, lng)``, as an ``h3.grid_disk`` (issue #337).

    Replaces the old square-grid ``grid_cells_in_bbox(bbox_around(...), ...)`` two-step - H3's
    own ring-distance IS a disk around a point, so there's no bbox middleman needed. ``k`` (ring
    count) is sized from the resolution's average edge length with one extra ring of slack:
    ``rank_destinations`` re-filters every candidate against the exact haversine distance anyway
    (see its ``keep()``), so this only has to be a safe superset, never exact - under-covering
    would silently drop a legitimate destination near the search boundary, which over-covering
    (a handful of extra empty regions the SQL allowlist filter discards for free) cannot.
    """
    origin = h3.latlng_to_cell(lat, lng, h3_resolution)
    edge_km = h3.average_hexagon_edge_length(h3_resolution, unit="km")
    k = math.ceil(radius_km / edge_km) + 1
    return h3.grid_disk(origin, k)


def cells_along_segment(
    lat1: float, lng1: float, lat2: float, lng2: float, corridor_km: float, h3_resolution: int
) -> list[str]:
    """Every H3 cell within ``corridor_km`` of the straight line ``1 -> 2`` (issue #337).

    The corridor analogue of :func:`cells_in_radius` - a single disk can't cover a segment
    longer than its own radius, so this unions disks sampled every ``corridor_km`` along the
    line (straight lat/lng interpolation, the same flat-degree approximation
    :func:`project_to_plane` already uses at this scale). Consecutive disks overlap by
    construction (adjacent samples are exactly ``corridor_km`` apart, each disk's own radius),
    so the union has no gaps. Like :func:`cells_in_radius`, a safe superset is enough -
    ``rank_destinations_corridor`` re-filters every candidate against the exact perpendicular
    offset (see its ``keep()``).
    """
    total_km = haversine_km(lat1, lng1, lat2, lng2)
    n_samples = max(2, math.ceil(total_km / corridor_km) + 1)
    cells: set[str] = set()
    for i in range(n_samples):
        t = i / (n_samples - 1)
        sample_lat = lat1 + t * (lat2 - lat1)
        sample_lng = lng1 + t * (lng2 - lng1)
        cells.update(cells_in_radius(sample_lat, sample_lng, corridor_km, h3_resolution))
    return list(cells)


def grid_cell_center(cell_id: str) -> tuple[float, float]:
    """Center point of an H3 ``cell_id`` - ``(lat, lng)``. Resolution isn't needed as a separate
    argument (unlike the old degree grid's ``"{ilat}_{ilng}"`` key): an H3 index carries its own
    resolution, so the cell id alone is enough to recover its center."""
    return h3.cell_to_latlng(cell_id)


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
