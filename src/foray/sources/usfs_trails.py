"""Authoritative USFS foot-trail ingest from the Trail_NFS ArcGIS layer (issue #335 PR 3).

OSM can't reliably give legal-access or seasonal attributes for national-forest trails; today's
proxies for that in ``trails.py`` are all OSM-derived guesses (``ref``/``operator`` heuristics,
``barrier`` nodes, the ``_walk_in`` tag derivation). The USFS Enterprise Data Warehouse publishes
``Trans_Trail_NFS_Publish`` - the authoritative national foot/stock trail network, including
trails OSM is missing entirely (e.g. Lost Man Creek Trail). This module pulls that layer only
(``EDW_TrailNFSPublish_01/MapServer/0``); the MVUM roads layer (which carries the OHV-legality /
open-season matrix) and OSM/USFS dedup are follow-up PRs - see TODO.md R2.

Rows land in the same ``trails`` table as the OSM ingest (``source='usfs'`` keeps them distinct;
``kind='path'`` so ``trails_near``/``nearest_trail`` - which filter on ``kind``, not ``source`` -
pick them up unchanged). This is envelope + paging over one ArcGIS layer for the whole configured
coverage, cloning ``land.py``'s pattern (a national service, not tileable the way Overpass is) -
NOT ``trails.py``'s per-region Overpass tiling. One-shot per ``_USFS_TRAILS_VERSION``: skips once
``usfs_trails:coverage:v{N}`` is in ``ingest_log``, same self-heal pattern as land/camps/dispersed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from itertools import pairwise
from typing import Any

import httpx
import psycopg

from foray.cache import connection, is_ingested, record_ingest, upsert_trails
from foray.config import Settings, coverage_envelope
from foray.geo import haversine_km
from foray.sources.http import SOURCE_ERRORS, USER_AGENT

logger = logging.getLogger(__name__)

_QUERY_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_TrailNFSPublish_01/MapServer/0/query"
_SOURCE_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_TrailNFSPublish_01/MapServer/0"
# Terrestrial trails only - SNOW/WATER trail_type rows aren't a foraging surface.
_WHERE = "TRAIL_TYPE='TERRA'"

_PAGE_SIZE = 1000
# Matches land.py's ownership-polygon generalization; a national trail network's full vertex
# detail is unnecessary for the map and would balloon the cached geometry.
_SIMPLIFY_DEG = 0.0001
_MAX_POINTS_PER_LINE = 60

_ID_FIELD = "TRAIL_CN"
_FIELDS = (
    _ID_FIELD,
    "TRAIL_NAME",
    "TRAIL_CLASS",
    "TRAIL_SURFACE",
    "MANAGING_ORG",
    "NATIONAL_TRAIL_DESIGNATION",
)

# Bump when `_WHERE`/`_FIELDS` changes what's pulled, or `_attrs` changes what's kept - both the
# coverage-wide marker below fold this in so an already-ingested deployment re-pulls once, same
# idea as `land._LAND_SOURCES_VERSION` / `trails._TRAILS_QUERY_VERSION`.
_USFS_TRAILS_VERSION = 1


def _get(props: dict[str, Any], field: str) -> Any:
    """Case-insensitive property lookup - ArcGIS geojson lowercases requested field names."""
    if field in props:
        return props[field]
    lowered = field.lower()
    for key, value in props.items():
        if key.lower() == lowered:
            return value
    return None


def _out_fields() -> str:
    return ",".join(_FIELDS)


def _line_coords(geometry: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """GeoJSON LineString/MultiLineString coordinates -> list of (lat, lng) polylines."""
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind == "LineString" and coords:
        return [[(lat, lng) for lng, lat in coords]]
    if kind == "MultiLineString" and coords:
        return [[(lat, lng) for lng, lat in line] for line in coords if line]
    return []


def _sample(coords: list[tuple[float, float]], count: int) -> list[tuple[float, float]]:
    """Thin a vertex list to at most ``count`` evenly-spaced points, keeping first and last."""
    if len(coords) <= count:
        return coords
    step = (len(coords) - 1) / (count - 1)
    return [coords[round(index * step)] for index in range(count)]


def _to_lnglat(coords: list[tuple[float, float]]) -> list[list[float]]:
    return [[lng, lat] for lat, lng in coords]


def _length_km(lines: list[list[tuple[float, float]]]) -> float | None:
    """Great-circle length of the full (un-thinned) polyline set - never trust `gis_miles`, which
    the source can leave stale or mismatched with the actual geometry (see TODO.md R2)."""
    total = sum(haversine_km(a[0], a[1], b[0], b[1]) for line in lines for a, b in pairwise(line))
    return round(total, 3) if total > 0 else None


# USFS Trail Class 1 (minimal/undeveloped) -> 5 (fully developed) runs opposite to OSM's
# tracktype grade1 (best maintained) -> grade5 (roughest, barely a track) - the card/map read
# tracktype (see frontend/src/map/trail-attrs.ts), so invert here at ingest rather than teach the
# frontend a second grading scale.
def _tracktype(trail_class: Any) -> str | None:
    try:
        grade = int(trail_class)
    except (TypeError, ValueError):
        return None
    if not 1 <= grade <= 5:
        return None
    return f"grade{6 - grade}"


def _attrs(props: dict[str, Any]) -> dict[str, str] | None:
    """Detail attrs kept per trail row, in the vocab the card already renders (`trails._ATTR_TAGS`
    parallel: `tracktype`, plus the USFS-specific fields not part of that OSM vocab).

    Walk-in/`motor_vehicle` derivation and `seasonal` need the MVUM layer's OHV matrix, not
    carried by this (foot-trail-only) layer - deferred to that follow-up PR (TODO.md R2).
    """
    picked: dict[str, str] = {}
    trail_class = _get(props, "TRAIL_CLASS")
    if trail_class not in (None, ""):
        picked["trail_class"] = str(trail_class)
    tracktype = _tracktype(trail_class)
    if tracktype:
        picked["tracktype"] = tracktype
    for field, key in (
        ("TRAIL_SURFACE", "trail_surface"),
        ("MANAGING_ORG", "managing_org"),
        ("NATIONAL_TRAIL_DESIGNATION", "national_trail_designation"),
    ):
        value = _get(props, field)
        if value not in (None, ""):
            picked[key] = str(value)
    return picked or None


def _parse_feature(feature: dict[str, Any]) -> tuple[Any, ...] | None:
    """One ArcGIS GeoJSON feature -> a trails row tuple, or None if unusable."""
    geometry = feature.get("geometry")
    if not geometry:
        return None
    lines = _line_coords(geometry)
    if not lines:
        return None
    props = feature.get("properties") or {}
    trail_cn = _get(props, _ID_FIELD)
    if trail_cn in (None, ""):
        return None
    thinned = [_sample(line, _MAX_POINTS_PER_LINE) for line in lines if line]
    flat = [point for line in thinned for point in line]
    if not flat:
        return None
    center_lat, center_lng = flat[len(flat) // 2]
    trail_geometry: dict[str, Any] = (
        {"type": "LineString", "coordinates": _to_lnglat(thinned[0])}
        if len(thinned) == 1
        else {"type": "MultiLineString", "coordinates": [_to_lnglat(line) for line in thinned]}
    )
    name = _get(props, "TRAIL_NAME")
    name = str(name).strip() if name not in (None, "") else "USFS trail"
    attrs = _attrs(props)
    return (
        f"usfs:trail/{trail_cn}",
        name,
        "path",
        "usfs",
        _SOURCE_URL,
        center_lat,
        center_lng,
        json.dumps(trail_geometry, separators=(",", ":")),
        None,  # connects - trailhead rows only
        _length_km(lines),
        json.dumps(attrs, separators=(",", ":")) if attrs else None,
    )


def _iter_features(client: httpx.Client, envelope: tuple[float, float, float, float]) -> Iterator[dict[str, Any]]:
    """Yield every Trail_NFS feature ArcGIS returns for the envelope, paging until exhausted."""
    xmin, ymin, xmax, ymax = envelope
    offset = 0
    while True:
        resp = client.get(
            _QUERY_URL,
            params={
                "f": "geojson",
                "where": _WHERE,
                "geometry": f"{xmin},{ymin},{xmax},{ymax}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "outSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": _out_fields(),
                "returnGeometry": "true",
                "maxAllowableOffset": _SIMPLIFY_DEG,
                "resultOffset": offset,
                "resultRecordCount": _PAGE_SIZE,
            },
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        payload = resp.json()
        features = payload.get("features", [])
        if not features:
            return
        yield from features
        offset += len(features)
        if not payload.get("exceededTransferLimit") or len(features) < _PAGE_SIZE:
            return


def fetch_usfs_trails(
    envelope: tuple[float, float, float, float],
    *,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> list[tuple[Any, ...]]:
    """Fetch USFS foot trails within an (xmin, ymin, xmax, ymax) envelope, deduped by id.

    Best-effort like the other area sources: a failing/malformed response is logged and yields
    whatever was already parsed rather than aborting the whole ingest.
    """
    owns = client is None
    client = client or httpx.Client(timeout=60.0)
    by_id: dict[str, tuple[Any, ...]] = {}
    try:
        if progress_cb:
            progress_cb("Fetching USFS trails…", 0.0)
        for feature in _iter_features(client, envelope):
            row = _parse_feature(feature)
            if row is not None:
                by_id[row[0]] = row
    except SOURCE_ERRORS as error:
        logger.warning("usfs_trails: fetch failed (%s) - keeping %d rows parsed so far", error, len(by_id))
    finally:
        if owns:
            client.close()
    return list(by_id.values())


def ingest_usfs_trails_coverage(
    cfg: Settings,
    con: psycopg.Connection | None = None,
    *,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest USFS Trail_NFS foot trails across all of ``cfg.coverage`` in one envelope query.

    One-shot per ``_USFS_TRAILS_VERSION``, same self-heal pattern as
    ``land.ingest_public_land_coverage``: skips once ``usfs_trails:coverage:v{N}`` is recorded,
    bumping the version re-pulls on the next run.
    """
    key = f"usfs_trails:coverage:v{_USFS_TRAILS_VERSION}"
    with connection(con) as database:
        if is_ingested(database, key):
            logger.info("usfs_trails: coverage already ingested at v%d, skipping", _USFS_TRAILS_VERSION)
            if progress_cb:
                progress_cb("USFS trails already cached, skipping…", 100.0)
            return 0
        envelope = coverage_envelope(cfg.coverage)
        logger.info("usfs_trails: fetching Trail_NFS across %d coverage regions…", len(cfg.coverage))
        rows = fetch_usfs_trails(envelope, client=client, progress_cb=progress_cb)
        upsert_trails(database, rows)
        record_ingest(database, key, len(rows))
        database.execute("DELETE FROM ingest_log WHERE key LIKE %s AND key <> %s", ["usfs_trails:coverage:v%", key])
        logger.info("usfs_trails: cached %d USFS trails (coverage-wide)", len(rows))
        return len(rows)
