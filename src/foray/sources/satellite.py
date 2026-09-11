"""Esri satellite imagery for a selected destination's map fill (#293 follow-up).

Two rasters per region, fetched together and cached forever in ``region_satellite`` (regions
are a fixed grid - see ``cache.region_places``): the aerial photo (``World_Imagery``, no labels
baked in) plus a "hybrid" overlay composited from Esri's two standard reference layers -
``Reference/World_Transportation`` (roads, drawn first) under ``Reference/World_Boundaries_and_Places``
(borders/place names, drawn on top so label halos stay legible over the road lines) - flattened
into the one cached ``labels`` PNG so the frontend/schema still only deal with two rasters. All
three are real XYZ tile pyramids - confirmed live via
their ``MapServer?f=json`` capabilities (``"Map,Tilemap"``) - not the dynamic ``MapServer/export``
renderer sources/land and sources/fire use. That distinction matters a lot here:

- ``/export`` renders on demand (25-45s at any real resolution) and degrades hard under
  concurrent load (measured 95% failure rate at just 6 concurrent requests).
- The tile endpoints serve pre-rendered, CDN-cached 256x256 tiles - each one returns in well
  under a second, same as the OSM basemap tiles the frontend already fetches directly. A single
  region's live fetch (a handful of tile workers) sails through; a full-grid backfill fanning
  out across regions does get throttled (429s, dropped connections), so ``_fetch_tile`` retries
  with backoff and ``backfill_region_satellite`` keeps its concurrency modest and reports how
  many regions still failed.
- Critically, ``/export``'s label layer draws text at a fixed pixel height regardless of the
  requested resolution or ``dpi`` (confirmed live; this service also reports
  ``supportsDynamicLayers: false``, so there's no server-side override) - so a big single export
  either has illegibly tiny text or, at a legible size, blurs when stretched to match a sharp
  image layer. A real tile *pyramid* doesn't have this problem: each zoom level's tiles are
  authored with text sized correctly for that zoom, exactly like every other slippy map.

So instead of one big export call, ``fetch_region_satellite`` picks a zoom level for the
region's radius, fetches every tile covering its bounding box (the same box Leaflet's geodesic
``L.circle.getBounds()`` reports for that footprint - see ``geo.web_mercator_bbox_m``), and
stitches + crops them into one raster with :mod:`PIL.Image` - giving a result that's crisp *and*
legible at once, with no per-request render latency.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import httpx
import psycopg
from PIL import Image

from foray.cache import save_region_satellite
from foray.geo import KM_PER_DEG_LAT, web_mercator_bbox_m

logger = logging.getLogger(__name__)

IMAGE_TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ROADS_TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}"
)
LABELS_TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
)

# Named so the full-viewport satellite basemap proxy (issue #340, api/routes/tiles.py) can
# address any of the three layers by name instead of importing the URL templates directly.
TILE_LAYERS: dict[str, str] = {
    "image": IMAGE_TILE_URL,
    "roads": ROADS_TILE_URL,
    "labels": LABELS_TILE_URL,
}
# Esri serves imagery as JPEG and both reference layers as (alpha-carrying) PNG - passed through
# untouched by fetch_tile_bytes, so the proxy response's Content-Type must match.
_TILE_CONTENT_TYPE: dict[str, str] = {
    "image": "image/jpeg",
    "roads": "image/png",
    "labels": "image/png",
}

TILE_PX = 256

# Same Web Mercator radius as the tile services themselves (and Leaflet's default CRS - see
# geo.web_mercator_bbox_m) - needed here to convert a bbox in projected meters to the tile
# pyramid's global pixel space at a given zoom.
_WEB_MERCATOR_R = 6378137.0
_WEB_MERCATOR_CIRCUMFERENCE = 2 * math.pi * _WEB_MERCATOR_R

# Aim for a destination circle raster around this many pixels across - plenty sharp for the map
# fill without the tile-count (and request-count) blowing up: at this target, a typical 0.25deg
# region cell is on the order of 60-90 tiles per layer, not the 250+ a sharper target would need.
_TARGET_DIAMETER_PX = 2048


def _zoom_for_diameter(lat: float, diameter_m: float, target_px: int) -> int:
    meters_per_pixel_target = diameter_m / target_px
    meters_per_pixel_at_zoom_0 = _WEB_MERCATOR_CIRCUMFERENCE / TILE_PX * math.cos(math.radians(lat))
    zoom = math.log2(meters_per_pixel_at_zoom_0 / meters_per_pixel_target)
    return max(0, min(19, round(zoom)))


def _meters_to_global_pixel(x: float, y: float, zoom: int) -> tuple[float, float]:
    """Web Mercator meters -> the tile pyramid's global pixel space at ``zoom``.

    Standard slippy-map convention: pixel x grows east, pixel y grows *south* (opposite of
    Mercator's y, which grows north) - top-left of the pyramid is (-R*pi, +R*pi) in meters.
    """
    map_size = TILE_PX * (2**zoom)
    px = (x + math.pi * _WEB_MERCATOR_R) / _WEB_MERCATOR_CIRCUMFERENCE * map_size
    py = (math.pi * _WEB_MERCATOR_R - y) / _WEB_MERCATOR_CIRCUMFERENCE * map_size
    return px, py


# Esri's arcgisonline tile CDN throttles bursts with HTTP 429s and dropped connections. A
# full-grid backfill fans out hard: `concurrency` regions in parallel, each stitching 3 layers
# concurrently, each layer's `_stitched_crop` running its own pool of up to 5 tile workers - so
# the peak in-flight request count is roughly concurrency * 3 * 5. Those failures are
# recoverable - back off and retry - unlike a 4xx/5xx that means the tile genuinely isn't there.
# Kept small: the point is to ride out a rate-limit blip, not to hammer a down service.
_TILE_RETRY_ATTEMPTS = 4
_TILE_RETRY_BACKOFF_S = 0.75


def _fetch_tile_bytes(url_template: str, zoom: int, tile_x: int, tile_y: int, client: httpx.Client) -> bytes:
    url = url_template.format(z=zoom, x=tile_x, y=tile_y)
    for attempt in range(_TILE_RETRY_ATTEMPTS):
        last = attempt == _TILE_RETRY_ATTEMPTS - 1
        try:
            response = client.get(url)
            if response.status_code == 429 and not last:
                time.sleep(_TILE_RETRY_BACKOFF_S * 2**attempt + random.uniform(0, 0.25))
                continue
            response.raise_for_status()
            return response.content
        except httpx.TransportError:
            if last:
                raise
            time.sleep(_TILE_RETRY_BACKOFF_S * 2**attempt + random.uniform(0, 0.25))
    raise AssertionError("unreachable: the final attempt returns or raises")  # pragma: no cover


def _fetch_tile(url_template: str, zoom: int, tile_x: int, tile_y: int, client: httpx.Client) -> Image.Image:
    content = _fetch_tile_bytes(url_template, zoom, tile_x, tile_y, client)
    return Image.open(BytesIO(content)).convert("RGBA")


def fetch_tile_bytes(
    layer: str, zoom: int, tile_x: int, tile_y: int, *, client: httpx.Client | None = None
) -> tuple[bytes, str]:
    """Raw upstream bytes for one Esri tile addressed by the browser's own tile coordinates -
    the full-viewport satellite basemap proxy (issue #340, ``api.routes.tiles``), as opposed to
    ``fetch_region_satellite``'s server-computed stitch for one destination circle. No PIL
    re-encode: imagery stays JPEG and the reference layers keep their alpha channel exactly as
    Esri serves them. Raises ``httpx.HTTPError`` on failure; ``KeyError`` for an unknown layer
    (the route validates ``layer`` before calling this, so that should never surface).
    """
    url_template = TILE_LAYERS[layer]
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        content = _fetch_tile_bytes(url_template, zoom, tile_x, tile_y, client)
    finally:
        if owns:
            client.close()
    return content, _TILE_CONTENT_TYPE[layer]


def _stitched_crop(
    url_template: str, bbox: tuple[float, float, float, float], zoom: int, client: httpx.Client
) -> Image.Image:
    """Every tile covering ``bbox`` (Web Mercator meters) at ``zoom``, stitched and cropped to
    exactly ``bbox``'s pixel footprint - so the result aligns pixel-for-pixel with the same
    bbox regardless of where tile boundaries happen to fall."""
    xmin, ymin, xmax, ymax = bbox
    left, top = _meters_to_global_pixel(xmin, ymax, zoom)
    right, bottom = _meters_to_global_pixel(xmax, ymin, zoom)
    tile_x0, tile_y0 = int(left // TILE_PX), int(top // TILE_PX)
    tile_x1, tile_y1 = int((right - 1) // TILE_PX), int((bottom - 1) // TILE_PX)

    tile_xs = range(tile_x0, tile_x1 + 1)
    tile_ys = range(tile_y0, tile_y1 + 1)
    canvas = Image.new("RGBA", (len(tile_xs) * TILE_PX, len(tile_ys) * TILE_PX))

    def fetch_one(coords: tuple[int, int]) -> tuple[int, int, Image.Image]:
        tile_x, tile_y = coords
        return tile_x, tile_y, _fetch_tile(url_template, zoom, tile_x, tile_y, client)

    with ThreadPoolExecutor(max_workers=5) as executor:
        for tile_x, tile_y, tile in executor.map(fetch_one, ((x, y) for x in tile_xs for y in tile_ys)):
            canvas.paste(tile, ((tile_x - tile_x0) * TILE_PX, (tile_y - tile_y0) * TILE_PX))

    crop_box = (
        round(left - tile_x0 * TILE_PX),
        round(top - tile_y0 * TILE_PX),
        round(left - tile_x0 * TILE_PX + (right - left)),
        round(top - tile_y0 * TILE_PX + (bottom - top)),
    )
    return canvas.crop(crop_box)


def fetch_region_satellite(
    lat: float, lng: float, radius_m: float, *, client: httpx.Client | None = None
) -> tuple[bytes, bytes]:
    """Fetch ``(image_jpeg, labels_png)`` bytes for the disk of ``radius_m`` around ``(lat, lng)``,
    stitched from Esri's tile pyramids (see module docstring). All three layers are stitched at
    the same zoom, concurrently, since they're independent - keeping the worst-case cold-cache
    latency a live request pays down near one layer's fetch time (the coalescing lock in
    ``api.routes.layers._region_satellite_bytes`` means a live request only ever calls this once
    per region; ``backfill_region_satellite``'s own concurrency is across regions, not within
    one). Raises ``httpx.HTTPError`` on failure.
    """
    bbox = web_mercator_bbox_m(lat, lng, radius_m)
    zoom = _zoom_for_diameter(lat, radius_m * 2, _TARGET_DIAMETER_PX)
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            image_future = executor.submit(_stitched_crop, IMAGE_TILE_URL, bbox, zoom, client)
            roads_future = executor.submit(_stitched_crop, ROADS_TILE_URL, bbox, zoom, client)
            labels_future = executor.submit(_stitched_crop, LABELS_TILE_URL, bbox, zoom, client)
            image = image_future.result()
            roads = roads_future.result()
            labels = labels_future.result()
        image_bytes = BytesIO()
        image.convert("RGB").save(image_bytes, format="JPEG", quality=90)
        # Roads first, place-name labels on top - matches Esri's own "Imagery Hybrid" layer
        # order, so label halos stay legible over the road lines instead of the reverse.
        labels_bytes = BytesIO()
        Image.alpha_composite(roads, labels).save(labels_bytes, format="PNG")
        return image_bytes.getvalue(), labels_bytes.getvalue()
    finally:
        if owns:
            client.close()


def backfill_region_satellite(
    con: psycopg.Connection,
    *,
    cell_deg: float,
    max_regions: int | None = None,
    concurrency: int = 4,
    refresh: bool = False,
    progress_cb: Callable[[str, int, int], None] | None = None,
) -> tuple[int, int]:
    """Fetch + cache satellite imagery for every region that doesn't have it yet. Returns
    ``(cached, failed)``.

    Regions come from the `regions` table (materialized phenology - only cells with at least one
    observation exist there), so this backfills exactly the set of destinations the app can
    actually show, not the whole globe. ``refresh=True`` clears `region_satellite` first, so a
    change to what a region's raster should contain (bbox, zoom, tile sources, compositing) is
    re-fetched for every region instead of only new ones (`save_region_satellite` is
    `ON CONFLICT DO NOTHING`).

    Esri's tile CDN throttles a wide fan-out (429s, dropped connections), so `concurrency`
    stays modest and `_fetch_tile` retries with backoff; a region whose tiles still fail after
    that is logged and counted, not fatal - re-running picks it up. The caller decides what a
    high `failed` count means (the CLI exits non-zero when failures dominate).
    """
    if refresh:
        con.execute("TRUNCATE region_satellite")
    limit_sql = "LIMIT %s" if max_regions is not None else ""
    params: list[object] = [max_regions] if max_regions is not None else []
    rows = con.execute(
        "SELECT r.region_id, r.center_lat, r.center_lng FROM regions r "
        f"LEFT JOIN region_satellite s ON s.region_id = r.region_id WHERE s.region_id IS NULL {limit_sql}",
        params,
    ).fetchall()
    radius_m = (cell_deg * KM_PER_DEG_LAT * 1000) / 2
    total = len(rows)
    updated = 0
    failed = 0

    def fetch_one(row: tuple[str, float, float]) -> tuple[str, bytes, bytes] | None:
        region_id, lat, lng = row
        try:
            image, labels = fetch_region_satellite(lat, lng, radius_m)
        except httpx.HTTPError as error:
            logger.warning("backfill-satellite: %s failed (%s)", region_id, error)
            return None
        return region_id, image, labels

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for index, result in enumerate(executor.map(fetch_one, rows), start=1):
            if result is not None:
                region_id, image, labels = result
                save_region_satellite(con, region_id, image, labels)
                updated += 1
            else:
                failed += 1
            if progress_cb:
                progress_cb(result[0] if result else rows[index - 1][0], index, total)
    if failed:
        logger.warning("backfill-satellite: %d of %d regions failed", failed, total)
    return updated, failed
