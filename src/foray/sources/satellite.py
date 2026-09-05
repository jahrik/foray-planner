"""Esri satellite imagery for a selected destination's map fill (#293 follow-up).

Two exports per region, fetched together and cached forever in ``region_satellite`` (regions
are a fixed grid - see ``cache.region_places``): the aerial photo (``World_Imagery``, no labels
baked in) plus its standard "hybrid" pairing, a transparent PNG of roads/borders/place names
(``Reference/World_Boundaries_and_Places``). Both no-key, CORS-open ArcGIS REST services, same
shape as ``sources.land``/``sources.fire``. Esri's server clamps the requested size to its own
cap regardless of what's asked for - confirmed live, a ``size=8192,8192`` request still comes
back 4096x4096 - so ``MAX_PX`` requests exactly that ceiling.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg

from foray.cache import save_region_satellite
from foray.geo import KM_PER_DEG_LAT, web_mercator_bbox_m

logger = logging.getLogger(__name__)

IMAGE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"
LABELS_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/export"
)

# Esri's own server-side cap (see module docstring) - requesting more just wastes a round trip.
MAX_PX = 4096


def _export_params(bbox: tuple[float, float, float, float]) -> dict[str, str]:
    xmin, ymin, xmax, ymax = bbox
    return {
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bboxSR": "3857",
        "imageSR": "3857",
        "size": f"{MAX_PX},{MAX_PX}",
        "f": "image",
    }


def fetch_region_satellite(
    lat: float, lng: float, radius_m: float, *, client: httpx.Client | None = None
) -> tuple[bytes, bytes]:
    """Fetch ``(image_jpeg, labels_png)`` bytes for the disk of ``radius_m`` around ``(lat, lng)``.

    Each export takes 25-45s server-side at ``MAX_PX`` (Esri renders it on demand) - fine for a
    one-time backfill or cache-miss fetch, never something a page load should block on for long,
    hence the generous timeout. The two exports are independent, so they run concurrently rather
    than back-to-back - halves the worst-case cold-cache latency a live request pays (a live API
    request only ever calls this once per region - see the coalescing lock in
    ``api.routes.layers._region_satellite_bytes`` - so this is the only place that matters for
    single-request latency; ``backfill_region_satellite``'s own concurrency is across regions).
    Raises ``httpx.HTTPError`` on failure - callers decide whether to degrade (API route re-raises
    as a 502) or retry (the backfill CLI just moves on).
    """
    bbox = web_mercator_bbox_m(lat, lng, radius_m)
    owns = client is None
    client = client or httpx.Client(timeout=90.0)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            image_future = executor.submit(client.get, IMAGE_URL, params={**_export_params(bbox), "format": "jpg"})
            labels_future = executor.submit(
                client.get,
                LABELS_URL,
                params={**_export_params(bbox), "format": "png32", "transparent": "true"},
            )
            image_resp = image_future.result()
            labels_resp = labels_future.result()
        image_resp.raise_for_status()
        labels_resp.raise_for_status()
        return image_resp.content, labels_resp.content
    finally:
        if owns:
            client.close()


def backfill_region_satellite(
    con: psycopg.Connection,
    *,
    cell_deg: float,
    max_regions: int | None = None,
    concurrency: int = 8,
    progress_cb: Callable[[str, int, int], None] | None = None,
) -> int:
    """Fetch + cache satellite imagery for every region that doesn't have it yet.

    Regions come from the `regions` table (materialized phenology - only cells with at least one
    observation exist there), so this backfills exactly the set of destinations the app can
    actually show, not the whole globe. Each export is 25-45s of Esri render time, not bandwidth
    (see `fetch_region_satellite`), so fetches run `concurrency`-wide - the DB write for each
    result happens back on this one connection as results complete, serialized, since psycopg
    connections aren't thread-safe. A region that fails is logged and skipped, not fatal to the
    run - re-running only re-fetches the ones still missing.
    """
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
            if progress_cb:
                progress_cb(result[0] if result else rows[index - 1][0], index, total)
    return updated
