"""Map-layer reads around a region or an explicit point: camps, public land, trails, place."""

from __future__ import annotations

import logging
import threading

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from psycopg_pool import ConnectionPool

from foray import scoring
from foray.api.deps import (
    get_pool,
    get_state,
    parse_species,
    region_center,
    require_idle,
    resolve_device_id,
)
from foray.api.state import AppState
from foray.api_models import CampSite, FireNear, LandUnit, RegionPlace, Trail, TrailPath
from foray.cache import load_region_place as db_load_region_place
from foray.cache import load_region_places as db_load_region_places
from foray.cache import load_region_satellite as db_load_region_satellite
from foray.cache import save_region_place as db_save_region_place
from foray.cache import save_region_satellite as db_save_region_satellite
from foray.geo import KM_PER_DEG_LAT
from foray.sources import geocode, satellite, trails

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/camps")
def get_camps(
    region_id: str | None = Query(None),
    lat: float | None = Query(None),
    lng: float | None = Query(None),
    radius_km: float = Query(40.0),
    free_only: bool = Query(False),
    limit: int | None = Query(None, gt=0),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> list[CampSite]:
    """Campsites near a region (by id) or an explicit lat/lng, free-first by distance.

    ``limit`` caps the ranked result, e.g. a destination card's Campgrounds tab."""
    require_idle(state)
    if region_id is not None:
        center_lat, center_lng = region_center(region_id, state.cfg)
    elif lat is not None and lng is not None:
        center_lat, center_lng = lat, lng
    else:
        raise HTTPException(400, "provide `region_id` or both `lat` and `lng`")
    with pool.connection() as conn:
        sites = scoring.camps_near(
            conn,
            lat=center_lat,
            lng=center_lng,
            radius_km=radius_km,
            free_only=free_only,
            limit=limit,
        )
    return [CampSite.model_validate(site) for site in sites]


@router.get("/api/land")
def get_land(
    region_id: str | None = Query(None),
    lat: float | None = Query(None),
    lng: float | None = Query(None),
    radius_km: float = Query(40.0),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> list[LandUnit]:
    """Public-land ownership polygons near a region (by id) or an explicit lat/lng."""
    require_idle(state)
    if region_id is not None:
        center_lat, center_lng = region_center(region_id, state.cfg)
    elif lat is not None and lng is not None:
        center_lat, center_lng = lat, lng
    else:
        raise HTTPException(400, "provide `region_id` or both `lat` and `lng`")
    with pool.connection() as conn:
        units = scoring.land_near(conn, lat=center_lat, lng=center_lng, radius_km=radius_km)
    return [LandUnit.model_validate(unit) for unit in units]


@router.get("/api/fire")
def get_fire(
    region_id: str | None = Query(None),
    lat: float | None = Query(None),
    lng: float | None = Query(None),
    radius_km: float = Query(60.0),
    status: str | None = Query(None, pattern="^(active|historical)$"),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> list[FireNear]:
    """Active fire perimeters/points and recent burn scars near a region (by id) or an explicit
    lat/lng (issue #227). ``status=active|historical`` filters; both by default. Geometry is
    included for the map overlay. Informational only - links the official incident page."""
    require_idle(state)
    if region_id is not None:
        center_lat, center_lng = region_center(region_id, state.cfg)
    elif lat is not None and lng is not None:
        center_lat, center_lng = lat, lng
    else:
        raise HTTPException(400, "provide `region_id` or both `lat` and `lng`")
    with pool.connection() as conn:
        fires = scoring.fire_near(
            conn, lat=center_lat, lng=center_lng, radius_km=radius_km, status=status, include_geometry=True
        )
    return [FireNear.model_validate(fire) for fire in fires]


@router.get("/api/destinations/places")
def get_region_places(
    region_ids: str = Query(..., description="comma-separated region ids"),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> dict[str, RegionPlace]:
    """Batch companion to ``/api/destinations/{region_id}/place`` (issue #301 F7): one request
    for a whole result page's card titles instead of ~one per card. Returns only the regions
    whose place name is already cached - the common case, since a grid cell's centroid never
    moves. Uncached regions are omitted; the caller falls back to the per-region endpoint
    (which does the throttled Nominatim round-trip) for whatever is left, so a cold cache is
    no slower than before and a warm one collapses ~75 requests into 1."""
    require_idle(state)
    ids = [part.strip() for part in region_ids.split(",") if part.strip()]
    if not ids:
        return {}
    with pool.connection() as conn:
        cached = db_load_region_places(conn, ids)
    return {region_id: RegionPlace(place_name=name) for region_id, name in cached.items()}


@router.get("/api/destinations/{region_id}/place")
def get_region_place(
    region_id: str,
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> RegionPlace:
    """The one notable place name for a destination card's title (issue #206) - a national
    forest, city, etc, whichever `geocode.notable_place_name` finds first for the region's
    centroid. Cached forever per region (`region_places`) since a grid cell's centroid
    never moves: the first card to render for a given region pays one Nominatim round-trip
    (throttled - see `geocode._throttle`), every later view of that region is a DB hit.
    A successful lookup that finds nothing notable nearby caches `None` too, so a remote
    region doesn't retry every time its card renders - a transient network/HTTP failure is
    NOT cached, so it gets retried on the next render instead of being stuck permanently
    titleless.
    """
    require_idle(state)
    with pool.connection() as conn:
        found, place_name = db_load_region_place(conn, region_id)
        if found:
            return RegionPlace(place_name=place_name)
        center_lat, center_lng = region_center(region_id, state.cfg)
        try:
            place_name = geocode.notable_place_name(center_lat, center_lng)
        except (httpx.HTTPError, ValueError) as error:
            # Transient (network/rate-limit) failure - don't cache it as a permanent "no
            # place found" negative result, or a region would never get a real answer.
            # Just answer this one request with no title; the next card render retries.
            logger.warning("place: reverse geocode failed for region %s (%s)", region_id, error)
            return RegionPlace(place_name=None)
        db_save_region_place(conn, region_id, place_name)
    return RegionPlace(place_name=place_name)


# The frontend's two <img> tags (image + labels) request this same region within milliseconds
# of each other, so a cache miss without coalescing would fire two full fetch_region_satellite
# round trips (each itself two Esri calls) at once - up to 4x the necessary load on Esri and up
# to 2x the latency either request actually needs. One lock per region_id, created on first use
# and never removed (a handful of live cache-miss regions at a time, not an unbounded set - the
# known region set is backfilled ahead of time by `foray backfill-satellite`) serializes that
# down to one fetch; the second caller's post-lock cache read then just returns what the first
# one saved instead of fetching again.
_satellite_fetch_locks: dict[str, threading.Lock] = {}
_satellite_fetch_locks_guard = threading.Lock()


def _satellite_fetch_lock(region_id: str) -> threading.Lock:
    with _satellite_fetch_locks_guard:
        return _satellite_fetch_locks.setdefault(region_id, threading.Lock())


def _region_satellite_bytes(region_id: str, state: AppState, pool: ConnectionPool) -> tuple[bytes, bytes]:
    """Cached ``(image, labels)`` bytes for a region, fetching + caching on first request.

    Cached forever per region (`region_satellite`), same "fixed grid, never re-resolve"
    reasoning as `get_region_place` above. A cache miss here fetches + stitches Esri tile-pyramid
    imagery (`satellite.fetch_region_satellite`, ~1s) rather than a slow live `/export` render -
    `foray backfill-satellite` pays that ahead of time for the known region set regardless, so
    this cold path should be rare in practice.
    """
    with pool.connection() as conn:
        cached = db_load_region_satellite(conn, region_id)
        if cached is not None:
            return cached
    with _satellite_fetch_lock(region_id):
        with pool.connection() as conn:
            # Re-check inside the lock: the request that was already fetching may have finished
            # and saved while this one was waiting its turn.
            cached = db_load_region_satellite(conn, region_id)
            if cached is not None:
                return cached
            center_lat, center_lng = region_center(region_id, state.cfg)
            radius_m = (state.cfg.cell_deg * KM_PER_DEG_LAT * 1000) / 2
            try:
                image, labels = satellite.fetch_region_satellite(center_lat, center_lng, radius_m)
            except httpx.HTTPError as error:
                logger.warning("satellite: fetch failed for region %s (%s)", region_id, error)
                raise HTTPException(502, "satellite imagery temporarily unavailable") from None
            db_save_region_satellite(conn, region_id, image, labels)
        return image, labels


# A day of browser caching - long enough that panning back to a region you just looked at is
# free, short enough that a server-side raster change (new tiles, corrected bbox, a
# `region_satellite` refresh) reaches every client within 24h on its own. Deliberately *not*
# `immutable`: the URL is stable but its bytes are not, and `immutable` told browsers never to
# revalidate, so a raster fix stayed invisible to anyone who'd already loaded the old one.
_SATELLITE_CACHE_CONTROL = "public, max-age=86400"


@router.get(
    "/api/destinations/{region_id}/satellite/image",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
def get_region_satellite_image(
    region_id: str,
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> Response:
    """A selected destination's aerial photo (#293 follow-up) - see `_region_satellite_bytes`."""
    require_idle(state)
    image, _labels = _region_satellite_bytes(region_id, state, pool)
    return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": _SATELLITE_CACHE_CONTROL})


@router.get(
    "/api/destinations/{region_id}/satellite/labels",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
def get_region_satellite_labels(
    region_id: str,
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> Response:
    """The same destination's transparent roads/place-labels overlay - see `_region_satellite_bytes`."""
    require_idle(state)
    _image, labels = _region_satellite_bytes(region_id, state, pool)
    return Response(content=labels, media_type="image/png", headers={"Cache-Control": _SATELLITE_CACHE_CONTROL})


@router.get("/api/trails")
def get_trails(
    request: Request,
    region_id: str | None = Query(None),
    lat: float | None = Query(None),
    lng: float | None = Query(None),
    radius_km: float = Query(40.0),
    kind: str | None = Query(None),
    limit: int | None = Query(None, gt=0),
    sort: scoring.TrailSort = "nearest",
    significant_only: bool = False,
    species: str = Query("all"),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> list[Trail]:
    """Trails near a region (by id) or an explicit lat/lng.

    ``kind``/``limit`` scope this to e.g. just the nearest 20 trailheads for a destination
    card's Trails tab, instead of every path/route/trailhead in the radius. ``sort`` is
    ``nearest`` (default), ``relevance`` (named-route / longer trail / target-genus finds along
    the line first), or ``longest``; ``significant_only`` drops the unnamed OSM connector stubs.
    ``species`` scopes the relevance obs-density term to the device's selected genera.

    Geometry is omitted (``with_geometry=False``): this feeds a name + distance row list, and
    selecting a row draws the real trail by fetching ``/api/trails/network`` for that one id.
    Returning every trail's full LineString here was megabytes of payload nothing rendered.
    """
    require_idle(state)
    if region_id is not None:
        center_lat, center_lng = region_center(region_id, state.cfg)
    elif lat is not None and lng is not None:
        center_lat, center_lng = lat, lng
    else:
        raise HTTPException(400, "provide `region_id` or both `lat` and `lng`")
    device_id, _is_new = resolve_device_id(request)
    with pool.connection() as conn:
        found = scoring.trails_near(
            conn,
            lat=center_lat,
            lng=center_lng,
            radius_km=radius_km,
            kind=kind,
            limit=limit,
            sort=sort,
            significant_only=significant_only,
            taxon_ids=parse_species(species, conn, device_id) or None,
            with_geometry=False,
        )
    return [Trail.model_validate(trail) for trail in found]


@router.get("/api/trails/network")
def get_trail_network(
    trail_id: str = Query(...),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> TrailPath:
    """The real trail for a selected trailhead - live OSM topology when available, otherwise
    the nearest cached path/route (see ``trails.resolve_trail_network``). ``trail_id`` is a
    query param, not a path segment - trail ids embed a literal ``/`` (``osm:node/123``)."""
    require_idle(state)
    with pool.connection() as conn:
        try:
            result = trails.resolve_trail_network(conn, trail_id, client=state.http_client)
        except LookupError as error:
            raise HTTPException(404, str(error)) from None
    if result is None:
        raise HTTPException(404, f"no trail found for trailhead {trail_id!r}")
    return TrailPath.model_validate(result)
