"""Scored-destination reads: ranked regions, per-region calendar, photos, and alerts."""

from __future__ import annotations

import datetime as dt

import psycopg
import requests
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
    ObservationThumbnail,
    ObservationThumbnailRequest,
    ObservationThumbnails,
    PreciseObservation,
    RecentObservation,
    RecentObservationsPage,
    RegionScore,
)
from foray.cache import (
    get_observation_thumbnail,
    get_observation_thumbnails,
    precise_observation_ids,
    save_observation_thumbnail,
)
from foray.sources import inat

router = APIRouter()

# issue #333 PR 2: marks the ranking/destination JSON endpoints as per-device, following the
# same constant-plus-header pattern tiles.py/layers.py already use for the tile/satellite binary
# responses. `private` (not `public`, unlike those) - home lookup is per-device via
# resolve_device_id's cookie, so a shared/CDN cache serving one device's response to another
# would be wrong. No `max-age` (Copilot review, PR #348): `POST /api/location` changes the
# device's home and immediately triggers a re-fetch of these same URLs - a `max-age=60` would
# let the browser serve the *previous* home's cached response for up to a minute instead of
# hitting the (already-fast, thanks to `rank_cache`) server. `no-cache` still names the intent
# (this is safe for a private single-user cache to keep, just always revalidate) without that
# risk. Only set on success: a 409 ("no data for this area yet") must not be cached.
_DESTINATIONS_CACHE_CONTROL = "private, no-cache"


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
    """Ranked destinations around the visitor's home for the chosen months and genera (see
    ``scoring.rank_destinations``)."""
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
                h3_resolution=cfg.h3_resolution,
                recent_weeks=cfg.recent_weeks,
                ttl_seconds=cfg.observability.ranking_cache_ttl_seconds,
            )
    except psycopg.errors.UndefinedTable:
        raise HTTPException(409, "no data for this area yet - click Fetch data") from None
    response.headers["Cache-Control"] = _DESTINATIONS_CACHE_CONTROL
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
    """The 12-month calendar for one region: total and per-genus record counts by month."""
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
    response.headers["Cache-Control"] = _DESTINATIONS_CACHE_CONTROL
    return {str(month): CalendarBucket.model_validate(bucket) for month, bucket in calendar.items()}


@router.get("/api/observations/photos")
def observation_photos(
    region_id: str,
    request: Request,
    response: Response,
    species: str = Query("all"),
    months: str | None = Query(None),
    weeks: int | None = Query(None, gt=0, le=520),
    offset: int = Query(0, ge=0),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> RecentObservationsPage:
    """A page of a region's recent research-grade observations with their displayable Creative Commons photos, newest
    first."""
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
                h3_resolution=cfg.h3_resolution,
                months=selected_months,
                weeks=weeks,
                offset=offset,
            )
    except psycopg.errors.UndefinedTable:
        raise HTTPException(409, "no data for this area yet - click Fetch data") from None
    response.headers["Cache-Control"] = _DESTINATIONS_CACHE_CONTROL
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
    """The "Active now" list: regions with observations of the selected genera in the trailing ``weeks``."""
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
                h3_resolution=cfg.h3_resolution,
                weeks=weeks or cfg.recent_weeks,
            )
    except psycopg.errors.UndefinedTable:
        return []
    response.headers["Cache-Control"] = _DESTINATIONS_CACHE_CONTROL
    return [AlertRegion.model_validate(region) for region in regions]


@router.get("/api/observations/precise")
def observations_precise(
    request: Request,
    response: Response,
    species: str = Query("all"),
    months: str | None = Query(None),
    weeks: int | None = Query(None, gt=0, le=520),
    lat: float | None = Query(None),
    lng: float | None = Query(None),
    radius_km: float | None = Query(None),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> list[PreciseObservation]:
    """Precise observations near an explicit lat/lng (a focused destination), falling back to
    home + its search radius when omitted - same `lat`/`lng`/`radius_km` override pattern as
    `/api/camps` and `/api/trails`. `weeks` replaces the month filter with the trailing window
    `/api/alerts` uses, for the "active now" sort (issue #312)."""
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
                weeks=weeks,
            )
    except psycopg.errors.UndefinedTable:
        return []
    response.headers["Cache-Control"] = _DESTINATIONS_CACHE_CONTROL
    return [PreciseObservation.model_validate(obs) for obs in observations]


# A cached thumbnail (or "no displayable photo") is re-checked after this long, so a photo added or
# relicensed on iNat eventually shows up.
_THUMBNAIL_MAX_AGE_DAYS = 30


@router.get("/api/observations/{obs_id}/thumbnail")
def observation_thumbnail(
    obs_id: int,
    response: Response,
    pool: ConnectionPool = Depends(get_pool),
) -> ObservationThumbnail | None:
    """One CC-licensed photo for a precise observation's map popup (issue #449), or null when iNat
    has none we may display. Fetched from iNat the first time a popup asks and cached in
    `observation_thumbnails` (the "none" answer too), so hovering across pins costs iNat at most
    one call per observation. Only serves observations already cached as precise, so this can't
    be used to proxy arbitrary iNat lookups."""
    with pool.connection() as conn:
        known = conn.execute("SELECT 1 FROM observations WHERE id = %s AND obscured = FALSE", [obs_id]).fetchone()
        if known is None:
            raise HTTPException(404, "not a cached precise observation")
        cached, thumbnail = get_observation_thumbnail(conn, obs_id, _THUMBNAIL_MAX_AGE_DAYS)
        if not cached:
            try:
                photos = inat.photos_for_observations([obs_id]).get(obs_id, [])
            except (requests.exceptions.RequestException, inat.InatQuotaExceeded):
                raise HTTPException(503, "iNaturalist is unavailable - try again shortly") from None
            thumbnail = inat.pick_thumbnail(photos)
            save_observation_thumbnail(conn, obs_id, thumbnail)
    response.headers["Cache-Control"] = "public, max-age=86400"
    return ObservationThumbnail.model_validate(thumbnail) if thumbnail else None


@router.post("/api/observations/thumbnails")
def observation_thumbnails(
    body: ObservationThumbnailRequest,
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> ObservationThumbnails:
    """Warm and return popup photos for many precise observations at once (issue #449), so a
    destination's pins already have theirs when one is hovered. Same rules as the per-pin
    endpoint - only cached precise observations, fetched from iNat once and cached (the "none"
    answer too) - but the missing ones are looked up together, ~200 per iNat request instead of
    one each. An iNat failure just leaves the missing ids out of the result (the client falls
    back to the per-pin endpoint), it doesn't fail the batch."""
    with pool.connection() as conn:
        precise = precise_observation_ids(conn, body.ids)
        ids = [obs_id for obs_id in dict.fromkeys(body.ids) if obs_id in precise]
        found = get_observation_thumbnails(conn, ids, _THUMBNAIL_MAX_AGE_DAYS)
    missing = [obs_id for obs_id in ids if obs_id not in found]
    # Prefetch is best effort, so it never queues: a sync request worker is scarce (the pool is
    # small, and iNat's 429 backoff can hold one for minutes) and a waiting prefetch would starve
    # unrelated requests, including the per-pin fallbacks. While another prefetch is fetching,
    # answer with what is cached and leave the rest to the per-pin endpoint. The lock also keeps
    # overlapping requests for one circle from fetching the same observations twice.
    if missing and state.thumbnail_lock.acquire(blocking=False):
        try:
            # Another request may have filled some in since the first read.
            with pool.connection() as conn:
                found = get_observation_thumbnails(conn, ids, _THUMBNAIL_MAX_AGE_DAYS)
            missing = [obs_id for obs_id in ids if obs_id not in found]
            if missing:
                try:
                    photos = inat.photos_for_observations(missing)
                except (requests.exceptions.RequestException, inat.InatQuotaExceeded):
                    photos = None
                if photos is not None:
                    with pool.connection() as conn:
                        for obs_id in missing:
                            thumbnail = inat.pick_thumbnail(photos.get(obs_id, []))
                            save_observation_thumbnail(conn, obs_id, thumbnail)
                            found[obs_id] = thumbnail
        finally:
            state.thumbnail_lock.release()
    return ObservationThumbnails(thumbnails={str(obs_id): found[obs_id] for obs_id in ids if obs_id in found})
