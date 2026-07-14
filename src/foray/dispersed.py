"""Dispersed-camping layer from OpenStreetMap (Overpass API).

Fetches OSM-tagged campable places (``tourism=camp_site`` / ``camp_pitch``, ``backcountry=yes``)
and caches them as ``campsites`` rows (``kind='reported'``) so they flow through the existing
``camps_near`` scoring and ``/api/camps`` plumbing untouched.

These are user-contributed OSM points, not a legal guarantee. Always verify with the managing
agency before camping (see AGENTS.md, "No claims").

Commercial camping apps (iOverlander, The Dyrt) are deliberately *not* used: iOverlander's terms
license its content for personal, non-commercial use only (no redistribution/storage), and The
Dyrt exposes no open API - so OSM is the only source we can legally cache and re-serve.

Like the campground and land ingests, a failing Overpass request is skipped rather than aborting
the whole refresh.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
import psycopg

from foray.cache import connect, is_area_covered, record_ingest, upsert_campsites
from foray.config import Config

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "foray-planner/0.1 (mushroom trip planner; +https://github.com/jahrik)"

_MIN_REQUEST_INTERVAL = 1.0


def _around(lat: float, lng: float, radius_m: float) -> str:
    return f"around:{radius_m:.0f},{lat},{lng}"


def _reported_query(lat: float, lng: float, radius_m: float) -> str:
    """Overpass QL for OSM-tagged campable places within the home disk."""
    around = _around(lat, lng, radius_m)
    return (
        "[out:json][timeout:120];"
        "("
        f'nwr["tourism"="camp_site"]({around});'
        f'nwr["tourism"="camp_pitch"]({around});'
        f'nwr["backcountry"="yes"]({around});'
        ");"
        "out center tags;"
    )


def _post_overpass(client: httpx.Client, query: str, *, attempts: int = 4, base_delay: float = 2.0) -> dict[str, Any]:
    """POST a query, backing off on Overpass's throttle (429) / timeout (504) responses."""
    resp: httpx.Response | None = None
    for attempt in range(1, attempts + 1):
        resp = client.post(OVERPASS_URL, data={"data": query}, headers={"User-Agent": USER_AGENT})
        if resp.status_code in (429, 504) and attempt < attempts:
            retry_after = resp.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else base_delay * 2 ** (attempt - 1)
            time.sleep(delay)
            continue
        break
    assert resp is not None
    resp.raise_for_status()
    return resp.json()


def _element_point(element: dict[str, Any]) -> tuple[float, float] | None:
    """(lat, lng) of an Overpass element - its own coords (node) or its `center` (way/rel)."""
    if element.get("lat") is not None and element.get("lon") is not None:
        return float(element["lat"]), float(element["lon"])
    center = element.get("center") or {}
    if center.get("lat") is not None and center.get("lon") is not None:
        return float(center["lat"]), float(center["lon"])
    return None


def _reported_name(tags: dict[str, Any]) -> str:
    """A descriptive fallback name when an OSM campable place has no `name` tag."""
    if tags.get("backcountry") == "yes":
        return "Backcountry campsite (OSM)"
    if tags.get("tourism") == "camp_pitch":
        return "Camp pitch (OSM)"
    return "Campsite (OSM)"


def _parse_reported(payload: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Overpass payload -> campsites rows for OSM-tagged campable places (kind='reported')."""
    rows: list[tuple[Any, ...]] = []
    for element in payload.get("elements", []):
        etype = element.get("type")
        eid = element.get("id")
        if etype not in ("node", "way", "relation") or eid is None:
            continue
        point = _element_point(element)
        if point is None:
            continue
        lat, lng = point
        tags = element.get("tags") or {}
        fee_tag = str(tags.get("fee") or "").strip().lower()
        if fee_tag == "no":
            free, fee = True, None
        elif fee_tag:
            free, fee = None, "fee required"
        else:
            free, fee = None, None
        rows.append(
            (
                f"osm:{etype}/{eid}",
                tags.get("name") or _reported_name(tags),
                "reported",
                fee,
                free,
                lat,
                lng,
                "osm",
                f"https://www.openstreetmap.org/{etype}/{eid}",
            )
        )
    return rows


def fetch_reported_campsites(
    *,
    lat: float,
    lng: float,
    radius_km: float,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> list[tuple[Any, ...]]:
    """Fetch OSM reported campsites near home."""
    owns = client is None
    client = client or httpx.Client(timeout=180.0)
    radius_m = radius_km * 1000.0
    reported: list[tuple[Any, ...]] = []
    try:
        try:
            if progress_cb:
                progress_cb("Fetching reported campsites…", 0.0)
            payload = _post_overpass(client, _reported_query(lat, lng, radius_m))
            reported = _parse_reported(payload)
            logger.info("dispersed: %d reported OSM campsites", len(reported))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            logger.warning("dispersed: reported-sites query failed (%s) - skipping", error)
    finally:
        if owns:
            client.close()
    return reported


def ingest_dispersed(
    cfg: Config,
    con: psycopg.Connection | None = None,
    *,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest OSM reported campsites into ``campsites``. Returns rows upserted."""
    own_con = con is None
    database = con if con is not None else connect()
    home = cfg.home
    if is_area_covered(database, "dispersed:", home.lat, home.lng, home.radius_km):
        logger.info("dispersed: already ingested for this area, skipping")
        if progress_cb:
            progress_cb("Dispersed camping already cached, skipping…", 100.0)
        if own_con:
            database.close()
        return 0
    try:
        logger.info("dispersed: fetching OSM camping layers within %.0f km of home…", home.radius_km)
        rows = fetch_reported_campsites(
            lat=home.lat,
            lng=home.lng,
            radius_km=home.radius_km,
            client=client,
            progress_cb=progress_cb,
        )
        upsert_campsites(database, rows)
        key = f"dispersed:{home.lat}:{home.lng}:{home.radius_km}"
        record_ingest(database, key, len(rows), lat=home.lat, lng=home.lng, radius_km=home.radius_km)
        logger.info("dispersed: cached %d reported sites", len(rows))
        return len(rows)
    finally:
        if own_con:
            database.close()
