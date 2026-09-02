"""``/api/plan`` - a corridor trip plan from start to destination."""

from __future__ import annotations

import datetime as dt
import logging

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
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> TripPlan:
    """Corridor trip plan: fruiting stops (with nearby camp + trail) from start to destination.

    ``destination`` is auto-picked (best-scoring region reachable from ``start``) when omitted.
    """
    require_idle(state)
    cfg = state.cfg
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    selected_months = parse_months(months) if months is not None else [dt.date.today().month]

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
            trip = scoring.plan_route(
                conn,
                months=selected_months,
                taxon_ids=parse_species(species, conn, device_id),
                cell_deg=cfg.cell_deg,
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
            )
    except psycopg.errors.UndefinedTable:
        raise HTTPException(409, "no data for this area yet - click Fetch data") from None
    return TripPlan.model_validate(trip)
