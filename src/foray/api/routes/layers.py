"""Map-layer reads around a region or an explicit point: camps, public land, trails, place."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg_pool import ConnectionPool

from foray import scoring
from foray.api.deps import get_pool, get_state, region_center, require_idle
from foray.api.state import AppState
from foray.api_models import CampSite, FireNear, LandUnit, RegionPlace, Trail, TrailPath
from foray.cache import load_region_place as db_load_region_place
from foray.cache import save_region_place as db_save_region_place
from foray.sources import geocode, trails

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


@router.get("/api/trails")
def get_trails(
    region_id: str | None = Query(None),
    lat: float | None = Query(None),
    lng: float | None = Query(None),
    radius_km: float = Query(40.0),
    kind: str | None = Query(None),
    limit: int | None = Query(None, gt=0),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> list[Trail]:
    """Trails near a region (by id) or an explicit lat/lng, nearest to the hotspot first.

    ``kind``/``limit`` scope this to e.g. just the nearest 20 trailheads for a destination
    card's Trails tab, instead of every path/route/trailhead in the radius.

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
    with pool.connection() as conn:
        found = scoring.trails_near(
            conn,
            lat=center_lat,
            lng=center_lng,
            radius_km=radius_km,
            kind=kind,
            limit=limit,
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
