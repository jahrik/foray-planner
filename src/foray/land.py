"""Public-land ownership ingest from ArcGIS REST feature services.

For the free-camping question ("can I sleep near here for free"), the first thing to know is
*who owns the ground*. **BLM** and **USFS** are the two agencies the dispersed-camping proxy
(slice 2b) will consider, so this module pulls those two ownership layers as GeoJSON and caches
the polygons for the map. It reports ownership and links the official source - nothing more; it
makes no claim about whether camping is permitted anywhere (see AGENTS.md).

Two authoritative ArcGIS layers, queried with an envelope around home:

* **BLM Surface Management Agency** - national ownership layer; filtered to the BLM-managed
  polygons (``ADMIN_AGENCY_CODE='BLM'``).
* **USFS Administrative Forest Boundaries** - national forest units (good ``FORESTNAME``).

Geometry is generalized server-side (``maxAllowableOffset``) so the cached polygons stay light
enough for the field map, and stored as GeoJSON text (see ``cache.public_land``) - the read
path never needs PostGIS geometry types. This is **ownership only**: it never asserts
that camping is legal, just shows the land and links the official source (see AGENTS.md).

No API key is needed. Like the campground ingest, a single source being unreachable is skipped
rather than aborting the whole refresh.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg

from foray.cache import connect, is_ingested, record_ingest, upsert_public_land
from foray.config import Config

logger = logging.getLogger(__name__)

USER_AGENT = "foray-planner/0.1 (mushroom trip planner; +https://github.com/jahrik)"

_KM_PER_DEG_LAT = 111.0
_PAGE_SIZE = 1000
# Server-side geometry generalization, in degrees (~0.005° ≈ 500 m). Keeps national-forest
# MultiPolygons small enough to cache and render on a phone; ownership shading needs no more.
_SIMPLIFY_DEG = 0.005


@dataclass(frozen=True)
class LandSource:
    """One ArcGIS ownership layer and how to read a unit name / id out of its features."""

    key: str  # short cache source tag, e.g. "blm"
    agency: str  # display agency, e.g. "BLM"
    query_url: str  # ArcGIS layer `.../query` endpoint
    where: str  # server-side filter (e.g. restrict to one agency)
    name_field: str  # property holding the unit name (matched case-insensitively)
    fallback_name: str  # used when the name property is absent/blank
    id_field: str = "OBJECTID"  # stable per-feature id (matched case-insensitively)

    @property
    def source_url(self) -> str:
        """Human-facing link to the source service (drop the `/query` verb)."""
        return self.query_url.removesuffix("/query")


# The two dispersed-camping-relevant ownership layers. Endpoints confirmed against the live
# services; if either moves, only these constants change.
SOURCES: tuple[LandSource, ...] = (
    LandSource(
        key="blm",
        agency="BLM",
        query_url=(
            "https://gis.blm.gov/arcgis/rest/services/lands/"
            "BLM_Natl_SMA_LimitedScale/MapServer/1/query"
        ),
        where="ADMIN_AGENCY_CODE='BLM'",
        name_field="ADMIN_UNIT_NAME",
        fallback_name="BLM land",
    ),
    LandSource(
        key="usfs",
        agency="USFS",
        query_url=(
            "https://apps.fs.usda.gov/arcx/rest/services/EDW/"
            "EDW_ForestSystemBoundaries_01/MapServer/0/query"
        ),
        where="1=1",
        name_field="FORESTNAME",
        fallback_name="National Forest",
    ),
)


def _envelope(lat: float, lng: float, radius_km: float) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) lon/lat box enclosing the home disk - the ArcGIS query bbox."""
    dlat = radius_km / _KM_PER_DEG_LAT
    km_per_deg_lng = _KM_PER_DEG_LAT * max(abs(_cos(lat)), 0.01)
    dlng = radius_km / km_per_deg_lng
    return (lng - dlng, lat - dlat, lng + dlng, lat + dlat)


def _cos(deg: float) -> float:
    import math

    return math.cos(math.radians(deg))


def _get(props: dict[str, Any], field: str) -> Any:
    """Case-insensitive property lookup - ArcGIS geojson lowercases requested field names."""
    if field in props:
        return props[field]
    lowered = field.lower()
    for key, value in props.items():
        if key.lower() == lowered:
            return value
    return None


def _bounds(coordinates: Any) -> tuple[float, float, float, float] | None:
    """Bounding box (min_lng, min_lat, max_lng, max_lat) of arbitrarily nested GeoJSON coords."""
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


def _parse_feature(source: LandSource, feature: dict[str, Any]) -> tuple[Any, ...] | None:
    """One ArcGIS GeoJSON feature -> a public_land row tuple, or None if unusable."""
    geometry = feature.get("geometry")
    if not geometry or not geometry.get("coordinates"):
        return None
    props = feature.get("properties") or {}
    feature_id = _get(props, source.id_field)
    if feature_id in (None, ""):
        return None
    bounds = _bounds(geometry["coordinates"])
    if bounds is None:
        return None
    min_lng, min_lat, max_lng, max_lat = bounds
    name = _get(props, source.name_field)
    unit = str(name).strip() if name not in (None, "") else source.fallback_name
    return (
        f"{source.key}:{feature_id}",
        source.agency,
        unit,
        source.key,
        source.source_url,
        min_lat,
        min_lng,
        max_lat,
        max_lng,
        json.dumps(geometry, separators=(",", ":")),
    )


def _iter_features(
    client: httpx.Client, source: LandSource, envelope: tuple[float, float, float, float]
) -> Iterator[dict[str, Any]]:
    """Yield every feature ArcGIS returns for the envelope, paging until the transfer limit."""
    xmin, ymin, xmax, ymax = envelope
    offset = 0
    while True:
        resp = client.get(
            source.query_url,
            params={
                "f": "geojson",
                "where": source.where,
                "geometry": f"{xmin},{ymin},{xmax},{ymax}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "outSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": f"{source.id_field},{source.name_field}",
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
        # ArcGIS flags a truncated page; without the flag, a short page means we're done.
        if not payload.get("exceededTransferLimit") or len(features) < _PAGE_SIZE:
            return


def fetch_public_land(
    *,
    lat: float,
    lng: float,
    radius_km: float,
    client: httpx.Client | None = None,
    sources: Iterable[LandSource] = SOURCES,
    progress_cb: Callable[[str, float], None] | None = None,
) -> list[tuple[Any, ...]]:
    """Fetch ownership polygons near home from each source, deduped by id.

    A source that fails is skipped so the others still ingest - ownership is best-effort
    context, never a hard dependency of the refresh. "Fails" covers both transport errors
    (``httpx.HTTPError``) and a service returning something other than well-formed GeoJSON
    (a decode error - ``ValueError`` - or an unexpected shape - ``KeyError``/``TypeError``).
    """
    owns = client is None
    client = client or httpx.Client(timeout=60.0)
    envelope = _envelope(lat, lng, radius_km)
    by_id: dict[str, tuple[Any, ...]] = {}
    try:
        sources_list = list(sources)
        total = len(sources_list)
        for index, source in enumerate(sources_list):
            if progress_cb:
                progress_cb(
                    f"Fetching {source.agency} land…",
                    ((index + 1) / total) * 100.0 if total else 100.0,
                )
            before = len(by_id)
            try:
                for feature in _iter_features(client, source, envelope):
                    row = _parse_feature(source, feature)
                    if row is not None:
                        by_id[row[0]] = row
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                logger.warning("land: source %s failed (%s) - skipping", source.key, error)
                continue  # skip this source; keep whatever the others returned
            logger.info("land: %s returned %d units", source.key, len(by_id) - before)
    finally:
        if owns:
            client.close()
    return list(by_id.values())


def ingest_public_land(
    cfg: Config,
    con: psycopg.Connection | None = None,
    *,
    client: httpx.Client | None = None,
    sources: Iterable[LandSource] = SOURCES,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest public-land ownership polygons into the cache. Returns rows upserted."""
    own_con = con is None
    database = con if con is not None else connect()
    home = cfg.home
    key = f"land:{home.lat}:{home.lng}:{home.radius_km}"
    if is_ingested(database, key):
        logger.info("land: already ingested for this area, skipping")
        if progress_cb:
            progress_cb("Public land already cached, skipping…", 100.0)
        if own_con:
            database.close()
        return 0
    try:
        logger.info("land: fetching BLM/USFS ownership within %.0f km of home…", home.radius_km)
        rows = fetch_public_land(
            lat=home.lat,
            lng=home.lng,
            radius_km=home.radius_km,
            client=client,
            sources=sources,
            progress_cb=progress_cb,
        )
        upsert_public_land(database, rows)
        key = f"land:{home.lat}:{home.lng}:{home.radius_km}"
        record_ingest(database, key, len(rows))
        logger.info("land: cached %d public-land units", len(rows))
        return len(rows)
    finally:
        if own_con:
            database.close()
