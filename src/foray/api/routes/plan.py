"""``/api/plan`` - a corridor trip plan from start to destination."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from psycopg_pool import ConnectionPool

from foray import scoring
from foray.api.deps import (
    get_pool,
    get_state,
    parse_months,
    parse_species,
    require_idle,
    resolve_device_id,
    resolve_home,
    set_device_cookie,
)
from foray.api.state import AppState
from foray.api_models import TripPlan
from foray.geo import grid_cell_center
from foray.scoring.models import StopPin
from foray.scoring.queries import PinKind
from foray.sources import geocode

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/plan")
def plan(
    request: Request,
    response: Response,
    months: str | None = Query(None),
    species: str = Query("all"),
    start: str | None = Query(None, max_length=200),
    destination: str | None = Query(None, max_length=200),
    corridor_km: float = Query(60.0, gt=0),
    max_stops: int = Query(5, ge=1, le=20),
    max_drive_km: float = Query(400.0, gt=0),
    camp_radius_km: float = Query(40.0, gt=0),
    require_free_camp: bool = Query(False),
    waypoints: str | None = Query(None, max_length=400),
    pin: Annotated[list[str] | None, Query()] = None,
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> TripPlan:
    """Corridor trip plan: fruiting stops (with nearby camp + trail) from start to destination.

    ``destination`` is auto-picked (best-scoring region reachable from ``start``) when omitted.
    ``waypoints``, when given, are the whole itinerary. Each ``pin`` is
    ``{region_id}:{camp|trail|land}:{feature_id}`` - a campground, trail or public-land parcel
    the user picked as that waypoint's exact stop point (issue #311; a parcel resolves to its
    entrance); a pin whose feature is no longer cached falls back
    to the region centroid.
    """
    require_idle(state)
    cfg = state.cfg
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    selected_months = parse_months(months) if months is not None else [dt.date.today().month]

    # Ordered region ids hand-picked from the shortlist ("+ Plan"), threaded in as required
    # stops. Region ids are H3 cells (issue #337) - their own library validates the shape (a
    # malformed or out-of-range hex string raises ValueError, a ValueError subclass) more
    # strictly than a shape regex over the old "ilat_ilng" format could, so there's no separate
    # bounds check needed the way the old degree-grid ids required.
    picked_waypoints: list[str] = []
    if waypoints:
        picked_waypoints = [part.strip() for part in waypoints.split(",") if part.strip()]
        if len(picked_waypoints) > 10:
            raise HTTPException(422, "waypoints must be up to 10 comma-separated region ids")
        for waypoint in picked_waypoints:
            try:
                grid_cell_center(waypoint)
            except ValueError:
                raise HTTPException(422, f"waypoint {waypoint!r} is not valid coordinates") from None

    # region_id -> (kind, feature_id). Region ids (H3 hex) and kinds never contain ':', so
    # splitting at most twice leaves feature ids like "osm:node/123" intact.
    pin = pin or []
    if len(pin) > len(picked_waypoints):
        raise HTTPException(422, "at most one pin per waypoint")
    pin_refs: dict[str, tuple[PinKind, str]] = {}
    for raw_pin in pin:
        region_id, kind, feature_id = ([*raw_pin.split(":", 2), "", ""])[:3]
        if region_id not in picked_waypoints or not feature_id or len(raw_pin) > 300:
            raise HTTPException(422, f"pin {raw_pin!r} must be <waypoint region id>:<camp|trail|land>:<feature id>")
        if region_id in pin_refs:
            raise HTTPException(422, f"waypoint {region_id!r} has more than one pin")
        if kind == "camp":
            pin_refs[region_id] = ("camp", feature_id)
        elif kind == "trail":
            pin_refs[region_id] = ("trail", feature_id)
        elif kind == "land":
            pin_refs[region_id] = ("land", feature_id)
        else:
            raise HTTPException(422, f"pin kind {kind!r} must be 'camp', 'trail' or 'land'")

    def resolve_point(query: str) -> tuple[float, float]:
        try:
            location = geocode.resolve(query)
        except (LookupError, ValueError) as error:
            raise HTTPException(404, str(error)) from None
        except Exception:  # network/geocoder failure - don't leak internals to the client
            logger.warning("plan: geocoding %r failed", query, exc_info=True)
            raise HTTPException(502, "geocoding failed") from None
        return location.lat, location.lng

    try:
        with pool.connection() as conn:
            home = resolve_home(conn, device_id, cfg)
            start_lat, start_lng = resolve_point(start) if start else (home.lat, home.lng)
            dest_lat, dest_lng = resolve_point(destination) if destination else (None, None)
            pins: dict[str, StopPin] = {}
            for region_id, (kind, feature_id) in pin_refs.items():
                # A savepoint per pin: a land parcel's entrance is real geometry work, and if it
                # ever hits the statement timeout the stop should fall back to its centroid
                # rather than the whole plan 500ing on an aborted transaction.
                try:
                    with conn.transaction():
                        # A land parcel's entrance is measured from the region's own cell centre.
                        resolved = scoring.resolve_pin(conn, kind, feature_id, near=grid_cell_center(region_id))
                except psycopg.errors.QueryCanceled:
                    logger.warning("plan: resolving pin %s:%s timed out - using the centroid", kind, feature_id)
                    continue
                if resolved is not None:
                    pins[region_id] = resolved
            trip = scoring.plan_route(
                conn,
                months=selected_months,
                taxon_ids=parse_species(species, conn, device_id),
                h3_resolution=cfg.h3_resolution,
                start_lat=start_lat,
                start_lng=start_lng,
                destination_lat=dest_lat,
                destination_lng=dest_lng,
                corridor_km=corridor_km,
                auto_pick_radius_km=home.radius_km,
                recent_weeks=cfg.recent_weeks,
                max_stops=max_stops,
                max_drive_km=max_drive_km,
                camp_radius_km=camp_radius_km,
                require_free_camp=require_free_camp,
                waypoints=picked_waypoints,
                pins=pins,
                ttl_seconds=cfg.observability.ranking_cache_ttl_seconds,
            )
    except psycopg.errors.UndefinedTable:
        raise HTTPException(409, "no data for this area yet - click Fetch data") from None
    return TripPlan.model_validate(trip)
