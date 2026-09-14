"""Full-viewport satellite basemap tile proxy (issue #340).

Unlike ``routes.layers``'s per-destination satellite fill (one stitched raster per region
circle, #293), this serves arbitrary ``{z}/{x}/{y}`` tiles as the browser pans/zooms - a real
MapLibre raster basemap, not a fixed-grid raster. No persistent cache table: Esri's own tile CDN
is already fast (sub-second, see ``sources.satellite``'s module docstring), so a long browser
``Cache-Control`` is enough - caching arbitrary world tiles forever in Postgres would be unbounded
growth for a problem we have no evidence of yet.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response

from foray.api.deps import get_state
from foray.api.state import AppState
from foray.sources import satellite
from foray.sources.http import Throttle

logger = logging.getLogger(__name__)

router = APIRouter()

# A full-viewport layer can fan out to far more concurrent tile requests than the fixed-grid
# region backfill (satellite.py's own docstring) ever generated, so this paces upstream fetches
# process-wide the same way land.py/fire.py pace their own Esri/ArcGIS calls.
_throttle = Throttle(min_interval=0.02)

# A shared, process-lifetime client - reused across every request instead of opening a fresh
# TCP/TLS connection per tile (a viewport fan-out is dozens of tiles at once). httpx.Client is
# safe for concurrent use across threads, and route handlers here are plain `def`s that Starlette
# already runs in its worker thread pool (see api/app.py's `to_thread` sizing note).
_client = httpx.Client(timeout=30.0)

# Esri's own tile pyramid tops out at z19 (matches the Leaflet map's own `maxZoom: 19`); reject
# anything outside the valid `{z}/{x}/{y}` space up front rather than spending a throttle slot
# and an upstream round-trip on a request that can never succeed.
_MAX_ZOOM = 19


def _in_range(z: int, x: int, y: int) -> bool:
    if not 0 <= z <= _MAX_ZOOM:
        return False
    span = 1 << z
    return 0 <= x < span and 0 <= y < span


# A week of browser caching, not `immutable` - Esri's imagery does update periodically (unlike a
# content-addressed asset), so a stale tile should still revalidate eventually rather than being
# cached forever.
_TILE_CACHE_CONTROL = "public, max-age=604800"


@router.get(
    "/api/tiles/satellite/{z}/{x}/{y}.jpg",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
def get_satellite_tile(z: int, x: int, y: int) -> Response:
    """One Esri World Imagery tile, proxied same-origin so the satellite basemap toggle (issue
    #340) stays under the existing CSP (`security.py`'s satellite fill is `'self'`-only)."""
    if not _in_range(z, x, y):
        raise HTTPException(400, "tile coordinates out of range")
    _throttle.wait()
    try:
        content, content_type = satellite.fetch_tile_bytes("image", z, x, y, client=_client)
    except httpx.HTTPError as error:
        logger.warning("satellite tile proxy: fetch failed for %d/%d/%d (%s)", z, x, y, error)
        raise HTTPException(502, "satellite imagery temporarily unavailable") from None
    return Response(content=content, media_type=content_type, headers={"Cache-Control": _TILE_CACHE_CONTROL})


# Async, unlike `_client` above: a trails viewport pan/zoom fans out to far more concurrent
# tile requests than the 3-service satellite basemap ever does, and route handlers are plain
# `def`s Starlette runs in AnyIO's worker thread pool - capped at 12 tokens (api/app.py), sized
# to the DB pool. A sync martin fetch blocking one of those for up to `timeout` seconds could
# starve ordinary DB-bound API requests during a heavy pan (Copilot review, PR #368). `async def`
# below awaits this client instead of occupying a thread-pool token while it waits on I/O.
_martin_client = httpx.AsyncClient(timeout=10.0)

# Vector tiles change whenever a trails ingest/backfill runs (unlike Esri's imagery, which is
# effectively static), so this stays well short of the satellite proxy's week-long cache - long
# enough that panning back over the same area is free, short enough that a refreshed layer
# reaches an open tab within the hour.
_TRAILS_TILE_CACHE_CONTROL = "public, max-age=3600"


@router.get(
    "/api/tiles/trails/{z}/{x}/{y}.pbf",
    response_class=Response,
    responses={200: {"content": {"application/vnd.mapbox-vector-tile": {}}}},
)
async def get_trails_tile(z: int, x: int, y: int, state: AppState = Depends(get_state)) -> Response:
    """One trails vector tile (issue #336 PR 1), proxied same-origin from the martin tile server
    so the frontend never talks to the docker-internal `martin_url` host directly."""
    if not state.cfg.martin_url:
        raise HTTPException(404, "trails vector tiles are not configured")
    if not _in_range(z, x, y):
        raise HTTPException(400, "tile coordinates out of range")
    try:
        upstream = await _martin_client.get(f"{state.cfg.martin_url}/trails/{z}/{x}/{y}")
        upstream.raise_for_status()
    except httpx.HTTPError as error:
        logger.warning("trails tile proxy: fetch failed for %d/%d/%d (%s)", z, x, y, error)
        raise HTTPException(502, "trails tiles temporarily unavailable") from None
    return Response(
        content=upstream.content,
        media_type="application/vnd.mapbox-vector-tile",
        headers={"Cache-Control": _TRAILS_TILE_CACHE_CONTROL},
    )
