"""Wildfire perimeters + burn scars from NIFC / MTBS ArcGIS feature services (issue #227).

Two angles, one table (``cache.fire_perimeters``):

* **Active wildfire** - a safety/access signal (WFIGS Current Interagency Fire Perimeters, plus
  Fire Locations for small/new fires with no perimeter yet). Fast cadence, **replace
  semantics**: each refresh, rows the source no longer lists are deleted
  (``cache.replace_fire_lane``) - a contained fire drops out of the active layer.
* **Recent burn scars** - a morel-opportunity signal (InterAgency Fire Perimeter History,
  windowed to the last 3 completed fire years + the current year, matching the burn-morel
  productivity curve). Slow cadence, plain upsert.
* **Burn severity** - optional enrichment joined onto either lane, applied by a separate bulk
  source (``foray.sources.ravg``, issue #335 PR 4) rather than fetched here. The MTBS severity
  service this module originally called live turned out to be dead (confirmed 2026-09-14 - see
  ``ravg.py``'s module docstring); RAVG replaces it. The layer works without any severity data
  too (``fire_year`` + acreage carry the recent scars on their own).

Cloned from ``land.py``'s ArcGIS pattern end to end: envelope query, server-side geometry
generalization, GeoJSON stored as text, a representative center cached (the `geom` GIST index
serves "fire near here"), one source unreachable is skipped rather than aborting the refresh.
Informational only - links the
official incident page, never asserts a road/forest closure (see AGENTS.md).

No API key. Endpoints are the public NIFC ArcGIS services; if one moves, only the constants here
change.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import psycopg

from foray.alerting import alert
from foray.cache import replace_fire_lane, upsert_fire_perimeters
from foray.config import Settings
from foray.geo import bbox_around
from foray.sources.http import USER_AGENT

logger = logging.getLogger(__name__)

_PAGE_SIZE = 1000
_SIMPLIFY_DEG = 0.005  # ~500 m server-side generalization, same as land.py
_HISTORY_YEARS_BACK = 3  # + current year = the burn-morel productivity window
# issue #332: minimum previously-cached row count that makes a 0-row replace-lane response
# suspicious rather than a legitimate "nothing active right now".
_EMPTY_RESPONSE_GUARD = 5


def _lane_row_count(con: psycopg.Connection, source_key: str) -> int:
    row = con.execute("SELECT count(*) FROM fire_perimeters WHERE source_key = %s", [source_key]).fetchone()
    return int(row[0]) if row else 0


# NIFC WFIGS (Wildland Fire Interagency Geospatial Services) + InterAgency Perimeter History,
# all on the NIFC ArcGIS Online org. Burn-severity enrichment is a separate bulk source, not
# fetched here - see foray.sources.ravg.
ACTIVE_PERIMETERS_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
)
ACTIVE_LOCATIONS_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Locations_Current/FeatureServer/0/query"
)
PERIMETER_HISTORY_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "InterAgencyFirePerimeterHistory_All_Years_View/FeatureServer/0/query"
)

LANE_ACTIVE = "wfigs_active"
LANE_POINTS = "wfigs_points"
LANE_HISTORY = "perimeter_history"

_SOURCE_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError)


def _envelope(cfg: Settings) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) lon/lat box over all coverage regions that carry a bbox,
    falling back to a disk around home when none do."""
    boxes = [region.bbox for region in cfg.coverage if region.bbox is not None]
    if boxes:
        return (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
    bbox = bbox_around(cfg.home.lat, cfg.home.lng, cfg.home.radius_km)
    return (bbox.min_lng, bbox.min_lat, bbox.max_lng, bbox.max_lat)


def _get(props: dict[str, Any], *names: str) -> Any:
    """Case-insensitive lookup over several candidate property names (ArcGIS field casing
    varies between the WFIGS and history layers)."""
    lowered = {key.lower(): value for key, value in props.items()}
    for name in names:
        if name.lower() in lowered:
            value = lowered[name.lower()]
            if value not in (None, ""):
                return value
    return None


def _bounds(coordinates: Any) -> tuple[float, float, float, float] | None:
    """Bounding box (min_lng, min_lat, max_lng, max_lat) of nested GeoJSON coords."""
    min_lng = min_lat = float("inf")
    max_lng = max_lat = float("-inf")
    found = False
    stack = [coordinates]
    while stack:
        item = stack.pop()
        if (
            isinstance(item, (list, tuple))
            and len(item) >= 2
            and isinstance(item[0], (int, float))
            and isinstance(item[1], (int, float))
        ):
            lng, lat = float(item[0]), float(item[1])
            min_lng, max_lng = min(min_lng, lng), max(max_lng, lng)
            min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
            found = True
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return (min_lng, min_lat, max_lng, max_lat) if found else None


def _epoch_to_date(value: Any) -> dt.date | None:
    """ArcGIS date fields come back as epoch milliseconds."""
    if value in (None, ""):
        return None
    try:
        return dt.datetime.fromtimestamp(int(value) / 1000, tz=dt.UTC).date()
    except (ValueError, OverflowError, OSError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _incident_url(props: dict[str, Any]) -> str | None:
    url = _get(props, "InciWebID", "IncidentURL", "LinkToWebsite", "COMPLEX_URL")
    if url and str(url).startswith("http"):
        return str(url)
    inciweb = _get(props, "InciWebID", "IncidentID")
    if inciweb and str(inciweb).isdigit():
        return f"https://inciweb.wildfire.gov/incident-information/{inciweb}"
    return "https://www.nifc.gov/fire-information/nfn"


def _feature_row(
    *,
    source_key: str,
    feature: dict[str, Any],
    status: str,
    is_point: bool,
) -> tuple[Any, ...] | None:
    """One ArcGIS GeoJSON feature -> a ``fire_perimeters`` row tuple (``cache._FIRE_COLUMNS``)."""
    geometry = feature.get("geometry")
    if not geometry or not geometry.get("coordinates"):
        return None
    props = feature.get("properties") or {}
    feature_id = _get(props, "OBJECTID", "poly_SourceOID", "irwin_UniqueFireIdentifier", "UniqueFireIdentifier")
    if feature_id in (None, ""):
        return None
    bounds = _bounds(geometry["coordinates"])
    if bounds is None:
        return None
    # The bbox itself is no longer persisted (issue #268 PR 5 - the `geom` GIST index serves
    # "fire near here"); its centroid is still the representative `center_lat`/`center_lng`.
    min_lng, min_lat, max_lng, max_lat = bounds
    name = _get(props, "poly_IncidentName", "IncidentName", "FIRE_NAME", "attr_IncidentName") or "Unnamed fire"
    fire_year = _get(props, "attr_FireDiscoveryDateTime", "FIRE_YEAR", "FIRE_YEAR_INT")
    year = None
    if isinstance(fire_year, (int, str)) and str(fire_year)[:4].isdigit() and len(str(fire_year)) == 4:
        year = int(str(fire_year)[:4])
    discovery = _epoch_to_date(_get(props, "attr_FireDiscoveryDateTime", "poly_DateCurrent", "DATE_CUR"))
    if year is None and discovery is not None:
        year = discovery.year
    return (
        f"{source_key}:{feature_id}",
        source_key,
        str(feature_id),
        _get(props, "irwin_IrwinID", "IRWINID", "irwin_UniqueFireIdentifier", "IRWIN_ID"),
        str(name).strip(),
        status,
        year,
        discovery,
        _to_float(_get(props, "attr_PercentContained", "PercentContained", "irwin_PercentContained")),
        _to_float(_get(props, "poly_GISAcres", "GISAcres", "GIS_ACRES", "attr_IncidentSize")),
        _incident_url(props),
        is_point,
        (min_lat + max_lat) / 2,
        (min_lng + max_lng) / 2,
        json.dumps(geometry, separators=(",", ":")),
        dt.datetime.now(dt.UTC),
    )


def _iter_features(
    client: httpx.Client, url: str, *, where: str, envelope: tuple[float, float, float, float]
) -> Iterator[dict[str, Any]]:
    """Page an ArcGIS layer's GeoJSON features for the envelope until the transfer limit."""
    xmin, ymin, xmax, ymax = envelope
    offset = 0
    while True:
        resp = client.get(
            url,
            params={
                "f": "geojson",
                "where": where,
                "geometry": f"{xmin},{ymin},{xmax},{ymax}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "outSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "true",
                "maxAllowableOffset": _SIMPLIFY_DEG,
                "resultOffset": offset,
                "resultRecordCount": _PAGE_SIZE,
            },
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        payload = resp.json()
        features = payload.get("features") or []
        if not features:
            return
        yield from features
        offset += len(features)
        if not payload.get("exceededTransferLimit") or len(features) < _PAGE_SIZE:
            return


def _fetch_lane(
    client: httpx.Client,
    url: str,
    *,
    source_key: str,
    status: str,
    where: str,
    envelope: tuple[float, float, float, float],
    is_point: bool = False,
) -> list[tuple[Any, ...]]:
    by_id: dict[str, tuple[Any, ...]] = {}
    for feature in _iter_features(client, url, where=where, envelope=envelope):
        row = _feature_row(source_key=source_key, feature=feature, status=status, is_point=is_point)
        if row is not None:
            by_id[row[0]] = row
    return list(by_id.values())


def refresh_fire(
    con: psycopg.Connection,
    cfg: Settings,
    *,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> dict[str, int]:
    """Refresh all three fire lanes over the coverage envelope (issue #227).

    Returns ``{"active", "points", "history"}`` row counts. Each lane is best-effort: one source
    failing is logged and skipped, the rest still refresh. The active and point lanes use replace
    semantics; history is a plain upsert. Burn-severity enrichment (``dominant_severity``) is
    applied by a separate bulk source (``foray.sources.ravg``, issue #335 PR 4), not fetched here
    - see this module's docstring for why."""
    owns = client is None
    client = client or httpx.Client(timeout=60.0, headers={"User-Agent": USER_AGENT})
    envelope = _envelope(cfg)
    this_year = dt.date.today().year
    counts = {"active": 0, "points": 0, "history": 0}
    try:
        lanes: list[tuple[str, str, str, str, bool, bool]] = [
            (LANE_ACTIVE, ACTIVE_PERIMETERS_URL, "active", "1=1", False, True),
            (LANE_POINTS, ACTIVE_LOCATIONS_URL, "active", "1=1", True, True),
            (
                LANE_HISTORY,
                PERIMETER_HISTORY_URL,
                "historical",
                f"FIRE_YEAR >= {this_year - _HISTORY_YEARS_BACK}",
                False,
                False,
            ),
        ]
        for index, (lane, url, status, where, is_point, replace) in enumerate(lanes):
            if progress_cb:
                progress_cb(f"Fetching {lane}…", index / (len(lanes) + 1) * 100.0)
            try:
                rows = _fetch_lane(
                    client, url, source_key=lane, status=status, where=where, envelope=envelope, is_point=is_point
                )
            except _SOURCE_ERRORS as error:
                logger.warning("fire: lane %s failed (%s) - skipping", lane, error)
                continue
            key = {LANE_ACTIVE: "active", LANE_POINTS: "points", LANE_HISTORY: "history"}[lane]
            cached = _lane_row_count(con, lane) if replace and not rows else 0
            if replace and not rows and cached >= _EMPTY_RESPONSE_GUARD:
                # issue #332: an empty response on a replace-semantics lane is indistinguishable
                # from "the source legitimately has nothing right now" (docstring above) unless
                # something was already cached - a transient WFIGS hiccup that returns 200 with
                # no features would otherwise wipe the whole active-fire layer to zero. Below the
                # guard, a small/empty lane clearing out is unremarkable and proceeds normally.
                logger.warning(
                    "fire: lane %s returned 0 rows but %d were cached - treating as a source "
                    "hiccup, keeping the cached rows instead of wiping the lane",
                    lane,
                    cached,
                )
                alert(
                    cfg,
                    "warning",
                    f"fire: lane {lane!r} returned 0 rows from an otherwise-populated cache - "
                    "skipped the replace-semantics wipe, kept the cached rows",
                )
                counts[key] = 0
                continue
            if replace:
                replace_fire_lane(con, lane, rows)
            else:
                upsert_fire_perimeters(con, rows)
            counts[key] = len(rows)
            logger.info("fire: lane %s -> %d rows", lane, len(rows))
    finally:
        if owns:
            client.close()
    return counts
