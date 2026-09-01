"""``/api/location`` - set or clear this visitor's saved home/radius override (issue #81)."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from foray import geocode
from foray.api.deps import get_pool, get_state, resolve_device_id, resolve_home, set_device_cookie
from foray.api.state import AppState
from foray.api_models import LocationResponse, StatusResponse
from foray.cache import delete_location as db_delete_location
from foray.cache import save_location as db_save_location
from foray.config import Home

logger = logging.getLogger(__name__)

router = APIRouter()


class LocationBody(BaseModel):
    query: str | None = Field(default=None, max_length=200)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    name: str | None = Field(default=None, max_length=200)
    radius_km: float | None = Field(default=None, gt=0, le=500)


@router.post("/api/location")
def set_location(
    body: LocationBody,
    request: Request,
    response: Response,
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> LocationResponse:
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    with pool.connection() as conn:
        current_home = resolve_home(conn, device_id, state.cfg)
        if body.lat is not None and body.lng is not None:
            # No client-supplied name: reverse-geocode server-side (issue #145) rather than
            # trusting the client to have done its own Nominatim lookup - keeps the User-
            # Agent/rate-limit policy in one place and avoids the browser sending precise
            # coordinates straight to a third party. Best-effort: a failed/slow reverse
            # lookup falls back to the coordinate string, same as before this endpoint did
            # any reverse geocoding at all.
            name = body.name
            if name is None:
                try:
                    name = geocode.reverse(body.lat, body.lng).name[:200]
                except (httpx.HTTPError, LookupError, ValueError):
                    logger.warning("location: reverse geocode failed for %s,%s", body.lat, body.lng, exc_info=True)
            home = Home(
                name=name or f"{body.lat:.4f}, {body.lng:.4f}",
                lat=body.lat,
                lng=body.lng,
                radius_km=body.radius_km or current_home.radius_km,
            )
        elif body.query:
            try:
                location = geocode.resolve(body.query)
            except (LookupError, ValueError) as error:
                raise HTTPException(404, str(error)) from None
            except Exception as error:  # network/geocoder failure
                raise HTTPException(502, f"geocoding failed: {error}") from None
            home = Home(
                name=body.name or location.name,
                lat=location.lat,
                lng=location.lng,
                radius_km=body.radius_km or current_home.radius_km,
            )
        else:
            raise HTTPException(400, "provide `query` or both `lat` and `lng`")

        db_save_location(
            conn, device_id=device_id, name=home.name, lat=home.lat, lng=home.lng, radius_km=home.radius_km
        )

    return LocationResponse(home=home)


@router.delete("/api/location")
def delete_location(
    request: Request,
    response: Response,
    pool: ConnectionPool = Depends(get_pool),
) -> StatusResponse:
    """Issue #81: let a visitor delete their saved home/radius override. Scoped to the
    caller's own device_id cookie - never touches another visitor's row."""
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    else:
        with pool.connection() as conn:
            db_delete_location(conn, device_id)
    return StatusResponse(status="deleted")
