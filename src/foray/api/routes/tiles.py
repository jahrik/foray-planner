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
from fastapi import APIRouter, HTTPException, Response

from foray.sources import satellite
from foray.sources.http import Throttle

logger = logging.getLogger(__name__)

router = APIRouter()

# A full-viewport layer can fan out to far more concurrent tile requests than the fixed-grid
# region backfill (satellite.py's own docstring) ever generated, so this paces upstream fetches
# process-wide the same way land.py/fire.py pace their own Esri/ArcGIS calls.
_throttle = Throttle(min_interval=0.02)

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
    _throttle.wait()
    try:
        content, content_type = satellite.fetch_tile_bytes("image", z, x, y)
    except httpx.HTTPError as error:
        logger.warning("satellite tile proxy: fetch failed for %d/%d/%d (%s)", z, x, y, error)
        raise HTTPException(502, "satellite imagery temporarily unavailable") from None
    return Response(content=content, media_type=content_type, headers={"Cache-Control": _TILE_CACHE_CONTROL})
