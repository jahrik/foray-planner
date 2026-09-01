"""Developed-campground ingest from the Recreation.gov RIDB API.

RIDB (https://ridb.recreation.gov/) is the authoritative dataset for *developed*
campgrounds - named sites with facilities. It needs a free API key, read from the
``RIDB_API_KEY`` environment variable (never committed; a gitignored ``.env`` locally, a
container/systemd env var on the server). If the key is absent, camps ingest is skipped
so the iNaturalist refresh still works.

The facilities search only accepts a point + radius (miles, capped), so a wide home radius
is covered by *tiling*: query circles laid on a grid dense enough to cover the whole disk,
then facilities are deduped by id and clipped to the true home radius with ``haversine_km``.

Dispersed (free, undeveloped) camping has no authoritative dataset and is a separate,
proxy-based layer (Epic 2, follow-up) - this module only handles developed campgrounds.
"""

from __future__ import annotations

import html
import logging
import math
import os
import re
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import psycopg

from foray.cache import connection, is_area_covered, record_ingest, upsert_campsites
from foray.config import Settings
from foray.geo import KM_PER_DEG_LAT, haversine_km
from foray.http import SOURCE_ERRORS, USER_AGENT, Throttle, retry_after_seconds

logger = logging.getLogger(__name__)

RIDB_FACILITIES = "https://ridb.recreation.gov/api/v1/facilities"

_KM_PER_MILE = 1.609344
# RIDB caps the facilities-search radius; 50 mi is the documented max. Query circles of this
# radius are tiled over the home disk (see `_query_centers`).
_QUERY_RADIUS_MI = 50.0
_PAGE_SIZE = 50  # RIDB's max page size for the facilities endpoint

# RIDB rate-limits at 50 requests/minute; a wide radius tiles into dozens of requests, so
# pace them to stay comfortably under (45/min) and back off on a 429.
_MIN_REQUEST_INTERVAL = 60.0 / 45.0

# The tiling grid grows O(radius^2); config allows radii up to 20000 km, which would
# otherwise queue hundreds of thousands of RIDB requests (issue #115). Cap the grid and keep
# the centers closest to home when a radius would exceed it, rather than growing unbounded.
_MAX_QUERY_CENTERS = 200

# Fee descriptions that explicitly signal no charge. We only ever *assert* free on one of
# these; anything else stays unknown (NULL) rather than guessing paid - see AGENTS.md.
_FREE_MARKERS = ("no fee", "no charge", "free of charge", "fee: none", "$0", "$0.00")

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def _clean_text(text: str | None) -> str | None:
    """Strip HTML tags/entities and collapse whitespace - RIDB fee fields ship raw markup."""
    if not text:
        return None
    stripped = _WHITESPACE.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()
    return stripped or None


def _free_from_fee(fee: str | None) -> bool | None:
    """TRUE only when the fee text explicitly says no charge; otherwise unknown (None)."""
    if not fee:
        return None
    text = fee.lower()
    if any(marker in text for marker in _FREE_MARKERS):
        return True
    return None


def _query_centers(lat: float, lng: float, radius_km: float, query_radius_km: float) -> list[tuple[float, float]]:
    """Grid of query-circle centers covering the home disk.

    Circles of ``query_radius_km`` on a square grid of that same spacing fully tile the
    plane (worst-case gap is spacing·√2/2 < radius), so the whole disk is covered. Centers
    whose circle cannot reach the disk are dropped.
    """
    spacing_km = query_radius_km
    dlat_deg = spacing_km / KM_PER_DEG_LAT
    km_per_deg_lng = KM_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01)
    dlng_deg = spacing_km / km_per_deg_lng
    steps = max(1, math.ceil(radius_km / spacing_km))

    centers: list[tuple[float, float]] = []
    for ilat in range(-steps, steps + 1):
        for ilng in range(-steps, steps + 1):
            center_lat = lat + ilat * dlat_deg
            center_lng = lng + ilng * dlng_deg
            # Keep the center only if its query circle can overlap the home disk.
            if haversine_km(lat, lng, center_lat, center_lng) <= radius_km + query_radius_km:
                centers.append((center_lat, center_lng))

    if len(centers) > _MAX_QUERY_CENTERS:
        centers.sort(key=lambda center: haversine_km(lat, lng, center[0], center[1]))
        logger.warning(
            "camps: radius %.0f km needs %d RIDB query circles, capping at %d closest to home",
            radius_km,
            len(centers),
            _MAX_QUERY_CENTERS,
        )
        centers = centers[:_MAX_QUERY_CENTERS]
    return centers


def _parse_facility(record: dict[str, Any]) -> tuple[Any, ...] | None:
    """RIDB facility record -> a campsites row tuple, or None if it lacks usable coords."""
    facility_id = record.get("FacilityID")
    raw_lat = record.get("FacilityLatitude")
    raw_lng = record.get("FacilityLongitude")
    if not facility_id or raw_lat in (None, "") or raw_lng in (None, ""):
        return None
    lat, lng = float(raw_lat), float(raw_lng)
    if lat == 0.0 and lng == 0.0:  # RIDB uses 0,0 as "no coordinates"
        return None
    fee = _clean_text(record.get("FacilityUseFeeDescription"))
    return (
        f"ridb:{facility_id}",
        record.get("FacilityName") or f"Facility {facility_id}",
        "campground",
        fee,
        _free_from_fee(fee),
        lat,
        lng,
        "ridb",
        f"https://www.recreation.gov/camping/campgrounds/{facility_id}",
    )


def _get_page(
    client: httpx.Client,
    throttle: Throttle,
    params: dict[str, Any],
    headers: dict[str, str],
    *,
    attempts: int = 5,
    base_delay: float = 2.0,
) -> httpx.Response:
    """GET one page, pacing requests and backing off on a 429 (honoring Retry-After)."""
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    resp = None
    for attempt in range(1, attempts + 1):
        throttle.wait()
        resp = client.get(RIDB_FACILITIES, params=params, headers=headers)
        if resp.status_code != 429 or attempt == attempts:
            break
        time.sleep(retry_after_seconds(resp, attempt, base_delay=base_delay))
    assert resp is not None
    resp.raise_for_status()
    return resp


def _iter_facilities(
    client: httpx.Client,
    throttle: Throttle,
    api_key: str,
    lat: float,
    lng: float,
    radius_mi: float,
) -> Iterator[dict[str, Any]]:
    """Yield every CAMPING facility RIDB returns for one query circle, paging by offset."""
    offset = 0
    while True:
        resp = _get_page(
            client,
            throttle,
            params={
                "latitude": lat,
                "longitude": lng,
                "radius": radius_mi,
                "activity": "CAMPING",
                "limit": _PAGE_SIZE,
                "offset": offset,
            },
            headers={"apikey": api_key, "User-Agent": USER_AGENT},
        )
        payload = resp.json()
        records = payload.get("RECDATA", [])
        if not records:
            return
        yield from records
        offset += len(records)
        total = payload.get("METADATA", {}).get("RESULTS", {}).get("TOTAL_COUNT")
        if (total is not None and offset >= int(total)) or len(records) < _PAGE_SIZE:
            return


def fetch_campsites(
    *,
    lat: float,
    lng: float,
    radius_km: float,
    api_key: str,
    client: httpx.Client | None = None,
    min_interval: float = _MIN_REQUEST_INTERVAL,
    progress_cb: Callable[[str, float], None] | None = None,
) -> list[tuple[Any, ...]]:
    """Fetch developed campgrounds within ``radius_km`` of home, deduped and clipped."""
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    throttle = Throttle(min_interval)
    query_radius_km = _QUERY_RADIUS_MI * _KM_PER_MILE
    by_id: dict[str, tuple[Any, ...]] = {}
    try:
        centers = _query_centers(lat, lng, radius_km, query_radius_km)
        total_centers = len(centers)
        for index, (center_lat, center_lng) in enumerate(centers):
            if progress_cb:
                progress_cb(
                    f"Fetching campgrounds ({index + 1}/{total_centers})…",
                    ((index + 1) / total_centers) * 100.0 if total_centers else 100.0,
                )
            for record in _iter_facilities(client, throttle, api_key, center_lat, center_lng, _QUERY_RADIUS_MI):
                row = _parse_facility(record)
                if row is None:
                    continue
                site_lat, site_lng = row[5], row[6]
                if haversine_km(lat, lng, site_lat, site_lng) <= radius_km:
                    by_id[row[0]] = row
    except SOURCE_ERRORS as error:
        # Match the land / trails / dispersed ingests: a RIDB outage (or a malformed page)
        # degrades to "no campgrounds this run" rather than aborting the whole refresh. Any
        # facilities already collected from earlier query circles are kept.
        logger.warning("camps: RIDB fetch failed (%s) - keeping %d sites gathered so far", error, len(by_id))
    finally:
        if owns:
            client.close()
    return list(by_id.values())


def ingest_campgrounds(
    cfg: Settings,
    con: psycopg.Connection | None = None,
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest developed campgrounds into the cache. Returns rows upserted (0 if no key)."""
    api_key = api_key or os.getenv("RIDB_API_KEY")
    if not api_key:
        logger.info("camps: RIDB_API_KEY unset - skipping campground ingest")
        return 0
    home = cfg.home
    with connection(con) as database:
        if is_area_covered(database, "camps:ridb:", home.lat, home.lng, home.radius_km):
            logger.info("camps: already ingested for this area, skipping")
            if progress_cb:
                progress_cb("Campgrounds already cached, skipping…", 100.0)
            return 0
        logger.info("camps: fetching developed campgrounds within %.0f km of home…", home.radius_km)
        rows = fetch_campsites(
            lat=home.lat,
            lng=home.lng,
            radius_km=home.radius_km,
            api_key=api_key,
            client=client,
            progress_cb=progress_cb,
        )
        upsert_campsites(database, rows)
        key = f"camps:ridb:{home.lat}:{home.lng}:{home.radius_km}"
        record_ingest(database, key, len(rows), lat=home.lat, lng=home.lng, radius_km=home.radius_km)
        logger.info("camps: cached %d campgrounds", len(rows))
        return len(rows)
