"""``/api/config`` - per-visitor home + server tuning constants."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from psycopg_pool import ConnectionPool

from foray.api.deps import get_pool, get_state, resolve_device_id, resolve_home, set_device_cookie
from foray.api.state import AppState
from foray.api_models import ConfigResponse
from foray.geo import h3_edge_length_km

router = APIRouter()


@router.get("/api/config")
def get_config(
    request: Request,
    response: Response,
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> ConfigResponse:
    cfg = state.cfg
    device_id, is_new = resolve_device_id(request)
    if is_new:
        set_device_cookie(request, response, device_id)
    with pool.connection() as conn:
        home = resolve_home(conn, device_id, cfg)
    return ConfigResponse(
        home=home,
        region_radius_km=h3_edge_length_km(cfg.h3_resolution),
        recent_weeks=cfg.recent_weeks,
        refreshing=state.refreshing,
        last_error=state.last_error,
        basemap_url=cfg.basemap_url,
        terrain_url=cfg.terrain_url,
        satellite_tiles_url=cfg.satellite_tiles_url,
        trails_tiles_url=cfg.trails_tiles_url if cfg.martin_url else "",
        land_tiles_url=cfg.land_tiles_url if cfg.martin_url else "",
        fire_tiles_url=cfg.fire_tiles_url if cfg.martin_url else "",
    )
