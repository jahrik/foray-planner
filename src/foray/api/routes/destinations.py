"""Scored-destination reads: ranked regions, per-region calendar, photos, and alerts."""

from __future__ import annotations

import datetime as dt

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
from foray.api_models import (
    AlertRegion,
    CalendarBucket,
    PreciseObservation,
    RecentObservation,
    RecentObservationsPage,
    RegionScore,
)
from foray.sources import inat

router = APIRouter()


@router.get("/api/destinations")
def destinations(
    request: Request,
    response: Response,
    months: str | None = Query(None),
    species: str = Query("all"),
    radius_km: float | None = Query(None),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> list[RegionScore]:
    require_idle(state)
    cfg = state.cfg
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    # No months given -> default to the current calendar month.
    selected_months = parse_months(months) if months is not None else [dt.date.today().month]
    try:
        with pool.connection() as conn:
            home = resolve_home(conn, device_id, cfg)
            ranked = scoring.rank_destinations(
                conn,
                months=selected_months,
                taxon_ids=parse_species(species, conn, device_id),
                home_lat=home.lat,
                home_lng=home.lng,
                radius_km=radius_km or home.radius_km,
                cell_deg=cfg.cell_deg,
                recent_weeks=cfg.recent_weeks,
            )
    except psycopg.errors.UndefinedTable:
        raise HTTPException(409, "no data for this area yet - click Fetch data") from None
    return [RegionScore.model_validate(region) for region in ranked]


@router.get("/api/calendar")
def calendar(
    region_id: str,
    request: Request,
    response: Response,
    species: str = Query("all"),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> dict[str, CalendarBucket]:
    require_idle(state)
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    try:
        with pool.connection() as conn:
            calendar = scoring.place_calendar(
                conn, region_id=region_id, taxon_ids=parse_species(species, conn, device_id)
            )
    except psycopg.errors.UndefinedTable:
        raise HTTPException(409, "no data for this area yet - click Fetch data") from None
    return {str(month): CalendarBucket.model_validate(bucket) for month, bucket in calendar.items()}


@router.get("/api/observations/photos")
def observation_photos(
    region_id: str,
    request: Request,
    response: Response,
    species: str = Query("all"),
    months: str | None = Query(None),
    offset: int = Query(0, ge=0),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> RecentObservationsPage:
    require_idle(state)
    cfg = state.cfg
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    # No months given -> default to the current calendar month, matching /api/destinations.
    selected_months = parse_months(months) if months is not None else [dt.date.today().month]
    try:
        with pool.connection() as conn:
            recent, has_more = scoring.recent_observations(
                conn,
                region_id=region_id,
                taxon_ids=parse_species(species, conn, device_id),
                cell_deg=cfg.cell_deg,
                months=selected_months,
                offset=offset,
            )
    except psycopg.errors.UndefinedTable:
        raise HTTPException(409, "no data for this area yet - click Fetch data") from None
    photos_by_obs = inat.photos_for_observations([obs["id"] for obs in recent])
    result = []
    for obs in recent:
        photos = [
            {"url": photo["url"], "license_code": photo["license_code"], "attribution": photo["attribution"]}
            for photo in photos_by_obs.get(obs["id"], [])
            if photo.get("license_code") in inat.DISPLAYABLE_PHOTO_LICENSES
        ]
        result.append(RecentObservation.model_validate({**obs, "photos": photos}))
    return RecentObservationsPage(observations=result, has_more=has_more)


@router.get("/api/alerts")
def get_alerts(
    request: Request,
    response: Response,
    species: str = Query("all"),
    weeks: int | None = Query(None),
    radius_km: float | None = Query(None),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> list[AlertRegion]:
    require_idle(state)
    cfg = state.cfg
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    try:
        with pool.connection() as conn:
            home = resolve_home(conn, device_id, cfg)
            regions = scoring.alerts(
                conn,
                taxon_ids=parse_species(species, conn, device_id),
                home_lat=home.lat,
                home_lng=home.lng,
                radius_km=radius_km or home.radius_km,
                cell_deg=cfg.cell_deg,
                weeks=weeks or cfg.recent_weeks,
            )
    except psycopg.errors.UndefinedTable:
        return []
    return [AlertRegion.model_validate(region) for region in regions]


@router.get("/api/observations/precise")
def observations_precise(
    request: Request,
    response: Response,
    species: str = Query("all"),
    months: str | None = Query(None),
    lat: float | None = Query(None),
    lng: float | None = Query(None),
    radius_km: float | None = Query(None),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> list[PreciseObservation]:
    """Precise observations near an explicit lat/lng (a focused destination), falling back to
    home + its search radius when omitted - same `lat`/`lng`/`radius_km` override pattern as
    `/api/camps` and `/api/trails`."""
    require_idle(state)
    if (lat is None) != (lng is None):
        raise HTTPException(400, "provide both `lat` and `lng`, or neither")
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    selected_months = parse_months(months) if months is not None else [dt.date.today().month]
    try:
        with pool.connection() as conn:
            home = resolve_home(conn, device_id, cfg=state.cfg)
            center_lat = lat if lat is not None else home.lat
            center_lng = lng if lng is not None else home.lng
            observations = scoring.precise_observations(
                conn,
                taxon_ids=parse_species(species, conn, device_id),
                lat=center_lat,
                lng=center_lng,
                radius_km=radius_km or home.radius_km,
                months=selected_months,
            )
    except psycopg.errors.UndefinedTable:
        return []
    return [PreciseObservation.model_validate(obs) for obs in observations]
