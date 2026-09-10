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
from collections.abc import Callable
from typing import Any

import httpx
import psycopg

from foray import cache
from foray.cache import upsert_campsites
from foray.config import Settings, coverage_envelope
from foray.sources import overpass
from foray.sources.http import SOURCE_ERRORS
from foray.sources.ingest_base import run_area_ingest
from foray.sources.trails import _tile_bboxes

logger = logging.getLogger(__name__)

# Bump when the Overpass selector set below changes: the marker ``dispersed:coverage:v{N}``
# stops matching and the next ``refresh --with dispersed --all`` cron re-pulls every tile
# (issue #306 workstream B, same self-heal as trails).
_DISPERSED_COVERAGE_VERSION = 1

_SELECTORS = ('nwr["tourism"="camp_site"]', 'nwr["tourism"="camp_pitch"]', 'nwr["backcountry"="yes"]')


def _reported_query(lat: float, lng: float, radius_m: float) -> str:
    """Overpass QL for OSM-tagged campable places within the home disk."""
    region = f"({overpass.around(lat, lng, radius_m)})"
    body = "".join(f"{selector}{region};" for selector in _SELECTORS)
    return f"[out:json][timeout:120];({body});out center tags;"


def _reported_query_bbox(min_lat: float, min_lng: float, max_lat: float, max_lng: float) -> str:
    """The same selectors as ``_reported_query`` over a state-sized bbox (longer server timeout)."""
    region = overpass.bbox(min_lat, min_lng, max_lat, max_lng)
    body = "".join(f"{selector}{region};" for selector in _SELECTORS)
    return f"[out:json][timeout:300];({body});out center tags;"


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
                None,  # reservable - OSM dispersed sites are first-come
                None,  # fee_low
                None,  # fee_high
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
            payload = overpass.post(client, _reported_query(lat, lng, radius_m))
            reported = _parse_reported(payload)
            logger.info("dispersed: %d reported OSM campsites", len(reported))
        except SOURCE_ERRORS as error:
            logger.warning("dispersed: reported-sites query failed (%s) - skipping", error)
    finally:
        if owns:
            client.close()
    return reported


def fetch_reported_campsites_bbox(
    *,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    client: httpx.Client | None = None,
    raise_on_error: bool = False,
) -> list[tuple[Any, ...]]:
    """Fetch OSM reported campsites within a state-sized bbox.

    ``raise_on_error`` mirrors ``trails.fetch_trails_bbox``: the coverage-wide ingest needs to
    tell an empty tile from a failed one to decide whether the whole run can be marked done.
    """
    owns = client is None
    client = client or httpx.Client(timeout=330.0)
    try:
        payload = overpass.post(client, _reported_query_bbox(min_lat, min_lng, max_lat, max_lng))
        return _parse_reported(payload)
    except SOURCE_ERRORS as error:
        if raise_on_error:
            raise
        logger.warning("dispersed: bbox query failed (%s) - skipping", error)
        return []
    finally:
        if owns:
            client.close()


def ingest_dispersed(
    cfg: Settings,
    con: psycopg.Connection | None = None,
    *,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest OSM reported campsites into ``campsites``. Returns rows upserted."""
    return run_area_ingest(
        cfg,
        con,
        prefix="dispersed:",
        label="dispersed",
        noun="Dispersed camping",
        fetch=lambda **kw: fetch_reported_campsites(client=client, **kw),
        upsert=upsert_campsites,
        progress_cb=progress_cb,
    )


def ingest_dispersed_coverage(
    cfg: Settings,
    con: psycopg.Connection | None = None,
    *,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest OSM reported campsites across all of ``cfg.coverage``, tiled like trails.

    Overpass can't take a whole-coverage query in one request, so the union envelope is carved
    into tiles (``trails._tile_bboxes``) and each tile's rows upserted as they arrive. One-shot
    per query version: skips once ``dispersed:coverage:v{N}`` is in ``ingest_log``. A tile
    failure leaves the run unmarked so the next cron retries the whole envelope. Returns rows
    upserted (a site straddling a tile edge is counted once per tile, same caveat as trails).
    """
    key = f"dispersed:coverage:v{_DISPERSED_COVERAGE_VERSION}"
    with cache.connection(con) as db:
        if cache.is_ingested(db, key):
            logger.info("dispersed: coverage-wide sites already ingested at v%d, skipping", _DISPERSED_COVERAGE_VERSION)
            if progress_cb:
                progress_cb("Dispersed camping already cached, skipping…", 100.0)
            return 0
        west, south, east, north = coverage_envelope(cfg.coverage)
        tiles = _tile_bboxes(south, west, north, east)
        logger.info("dispersed: fetching OSM reported campsites across coverage (%d tiles)…", len(tiles))
        total = 0
        had_failures = False
        for index, (tile_south, tile_west, tile_north, tile_east) in enumerate(tiles, start=1):
            if progress_cb:
                progress_cb(f"Fetching dispersed camping ({index}/{len(tiles)})…", (index / len(tiles)) * 100.0)
            try:
                rows = fetch_reported_campsites_bbox(
                    min_lat=tile_south,
                    min_lng=tile_west,
                    max_lat=tile_north,
                    max_lng=tile_east,
                    client=client,
                    raise_on_error=True,
                )
            except SOURCE_ERRORS as error:
                logger.warning("dispersed: tile %d/%d failed (%s) - will retry next run", index, len(tiles), error)
                had_failures = True
                continue
            upsert_campsites(db, rows)
            total += len(rows)
        pruned = cache.prune_campsites_outside_bounds(db, "osm", west, south, east, north)
        if had_failures:
            logger.warning("dispersed: coverage only partially ingested (%d rows) - not recording as done", total)
        else:
            cache.record_ingest(db, key, total)
            db.execute("DELETE FROM ingest_log WHERE key LIKE %s AND key <> %s", ["dispersed:coverage:v%", key])
        logger.info("dispersed: cached %d reported campsites coverage-wide (pruned %d outside envelope)", total, pruned)
        return total
