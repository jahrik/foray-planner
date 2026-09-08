"""Developed-campground ingest from the Recreation.gov RIDB API.

RIDB (https://ridb.recreation.gov/) is the authoritative dataset for *developed*
campgrounds - named sites with facilities. It needs a free API key, read from the
``RIDB_API_KEY`` environment variable (never committed; a gitignored ``.env`` locally, a
container/systemd env var on the server). If the key is absent, camps ingest is skipped
so the iNaturalist refresh still works.

RIDB's point+radius facilities search silently under-returns - it only matches facilities
whose own ``FacilityLatitude/Longitude`` is populated and near the point, which measured at
~1/3 of what is actually there (45 camping facilities within 50 mi of Eugene, OR by state
listing vs 13 by radius search). So the primary path is a **per-state** listing
(``facilities?state=XX&activity=CAMPING``, paged), with each facility clipped to the true
home radius by ``haversine_km``. The state set is the ``cfg.coverage`` regions whose bbox
reaches the home disk. Homes outside the US (no state resolves) fall back to the old
radius-tiling path.

Dispersed (free, undeveloped) camping has no authoritative dataset and is a separate,
proxy-based layer (tracked separately) - this module only handles developed campgrounds.
"""

from __future__ import annotations

import html
import logging
import math
import os
import re
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import httpx
import psycopg

from foray.cache import upsert_campsites
from foray.config import CoverageRegion, Settings
from foray.geo import KM_PER_DEG_LAT, haversine_km
from foray.sources.http import SOURCE_ERRORS, USER_AGENT, Throttle, retry_after_seconds
from foray.sources.ingest_base import run_area_ingest

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


# USPS codes for the ``cfg.coverage`` region names (US states + DC). The RIDB ``state`` filter
# wants the two-letter code; coverage regions carry only the name.
_STATE_CODES: dict[str, str] = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
}


def _states_for_disk(coverage: Sequence[CoverageRegion], lat: float, lng: float, radius_km: float) -> list[str]:
    """USPS codes of the coverage regions whose bbox reaches within ``radius_km`` of home.

    The home disk's bounding box is tested for overlap against each region's
    ``(west, south, east, north)`` bbox - a cheap superset of "the disk touches the state",
    which is all we need to decide whether to list that state's facilities. Regions with no
    bbox or no known code are skipped; an empty result means "not in the US, use tiling".
    """
    dlat = radius_km / KM_PER_DEG_LAT
    dlng = radius_km / (KM_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01))
    disk_w, disk_e = lng - dlng, lng + dlng
    disk_s, disk_n = lat - dlat, lat + dlat
    codes: list[str] = []
    for region in coverage:
        code = _STATE_CODES.get(region.name)
        if code is None or region.bbox is None:
            continue
        west, south, east, north = region.bbox
        if west <= disk_e and east >= disk_w and south <= disk_n and north >= disk_s:
            codes.append(code)
    return codes


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
    query: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """Yield every CAMPING facility RIDB returns for one query, paging by offset.

    ``query`` carries the scope filter - ``{"state": "OR"}`` for the per-state listing or
    ``{"latitude": .., "longitude": .., "radius": ..}`` for the fallback radius search.
    """
    offset = 0
    while True:
        resp = _get_page(
            client,
            throttle,
            params={**query, "activity": "CAMPING", "limit": _PAGE_SIZE, "offset": offset},
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


def _query_scopes(lat: float, lng: float, radius_km: float, states: Sequence[str]) -> list[tuple[str, dict[str, Any]]]:
    """(label, RIDB query params) pairs to page through - per-state when states resolved,
    otherwise the fallback radius tiling."""
    if states:
        return [(code, {"state": code}) for code in states]
    query_radius_mi = _QUERY_RADIUS_MI
    return [
        (f"{clat:.3f},{clng:.3f}", {"latitude": clat, "longitude": clng, "radius": query_radius_mi})
        for clat, clng in _query_centers(lat, lng, radius_km, _QUERY_RADIUS_MI * _KM_PER_MILE)
    ]


def fetch_campsites(
    *,
    lat: float,
    lng: float,
    radius_km: float,
    api_key: str,
    states: Sequence[str] = (),
    client: httpx.Client | None = None,
    min_interval: float = _MIN_REQUEST_INTERVAL,
    progress_cb: Callable[[str, float], None] | None = None,
) -> list[tuple[Any, ...]]:
    """Fetch developed campgrounds within ``radius_km`` of home, deduped and clipped.

    ``states`` is the USPS codes to list (``_states_for_disk``); empty falls back to the
    radius-tiling path for a non-US home.
    """
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    throttle = Throttle(min_interval)
    by_id: dict[str, tuple[Any, ...]] = {}
    scopes = _query_scopes(lat, lng, radius_km, states)
    try:
        for index, (label, query) in enumerate(scopes):
            if progress_cb:
                progress_cb(
                    f"Fetching campgrounds ({index + 1}/{len(scopes)}: {label})…",
                    ((index + 1) / len(scopes)) * 100.0 if scopes else 100.0,
                )
            for record in _iter_facilities(client, throttle, api_key, query):
                row = _parse_facility(record)
                if row is None:
                    continue
                site_lat, site_lng = row[5], row[6]
                if haversine_km(lat, lng, site_lat, site_lng) <= radius_km:
                    by_id[row[0]] = row
    except SOURCE_ERRORS as error:
        # Match the land / trails / dispersed ingests: a RIDB outage (or a malformed page)
        # degrades to "no campgrounds this run" rather than aborting the whole refresh. Any
        # facilities already collected from earlier scopes are kept.
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
    states = _states_for_disk(cfg.coverage, home.lat, home.lng, home.radius_km)
    logger.info(
        "camps: %s",
        f"listing {len(states)} states ({', '.join(states)})" if states else "no US state resolved - radius tiling",
    )
    return run_area_ingest(
        cfg,
        con,
        prefix="camps:ridb:",
        label="camps",
        noun="Campgrounds",
        fetch=lambda **kw: fetch_campsites(api_key=api_key, client=client, states=states, **kw),
        upsert=upsert_campsites,
        progress_cb=progress_cb,
    )
