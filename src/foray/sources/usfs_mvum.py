"""Bulk-snapshot USFS Motor Vehicle Use Map (MVUM) roads loader (issue #335 PR 3b).

Trail_NFS (PR 3a, ``usfs_trails.py``) added the authoritative foot/stock trail network but
deliberately left forest *roads* to the OSM-derived heuristics in ``trails.py``
(``highway=track``/``service=forestry`` -> ``kind='road'``, a ``barrier=gate`` node standing in for
a missing ``access``/``motor_vehicle`` tag - see ``scoring.queries._walk_in``). The USFS Enterprise
Data Warehouse's MVUM layer is the authoritative source those heuristics approximate: a
per-vehicle-class (passenger car, high-clearance, truck, ATV, motorcycle, ...) legal-access and
open-season matrix, unit by unit, kept current with the published Motor Vehicle Use Maps. Only
``SYMBOL`` values 1/2/3/4/11/12 are Forest Service System roads carrying that matrix (the service's
own field description); every other symbol value is drawn on the MVUM for context but isn't a
system road, so ``_WHERE`` excludes them.

Same stage/load split and ``ingest-bulk`` machinery as ``usfs_trails.py`` (see that module's
docstring) - this one queries ``EDW_MVUM_01/MapServer/1`` (roads) instead of
``EDW_TrailNFSPublish_01/MapServer/0``. Registered as its own bulk source (``usfs_mvum``, not
``usfs_trails``) so ``load_usfs_mvum``'s prune-to-exactly-what's-listed step only ever deletes MVUM
rows, never Trail_NFS's - both write ``trails`` rows with ``source='usfs_mvum'`` vs ``'usfs'``
respectively, kept distinct for exactly that reason (see ``cache.prune_trails_missing_from``).

Rows land in ``trails`` with ``kind='road'`` (never ``'path'`` - this layer is roads only), so
``trails_near``/``nearest_trail`` pick them up unchanged alongside OSM's own ``kind='road'`` rows.
Dedup between a USFS road and its OSM twin (two rows for the same physical road) happens at read
time in ``scoring.queries`` - prefer ``source`` starting with ``usfs`` within a few meters - not
here at ingest, matching Trail_NFS's own deferred-to-read-time approach.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import date
from itertools import pairwise
from typing import Any

import httpx
import psycopg
import pyarrow as pa
import shapely

from foray import cache, spaces
from foray.cache import record_ingest, upsert_trails
from foray.config import Settings
from foray.geo import haversine_km
from foray.sources.http import USER_AGENT

logger = logging.getLogger(__name__)

_QUERY_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_MVUM_01/MapServer/1/query"
_SOURCE_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_MVUM_01/MapServer/1"
# Only these SYMBOL values are Forest Service System roads carrying the OHV-legality matrix
# (the layer's own field description); everything else is contextual (non-FS, decommissioned, ...).
_WHERE = "symbol IN ('1','2','3','4','11','12')"

_PAGE_SIZE = 1000
_SIMPLIFY_DEG = 0.0001
_MAX_POINTS_PER_LINE = 60
_CHUNK_SIZE = 5000

_ID_FIELD = "rte_cn"
_FIELDS = (
    _ID_FIELD,
    "id",
    "name",
    "operationalmaintlevel",
    "surfacetype",
    "seasonal",
    "jurisdiction",
    "passengervehicle",
    "highclearancevehicle",
    "truck",
    "atv",
    "motorcycle",
)

_BULK_SNAPSHOT_FILENAME = "trails.parquet"
# Same layout as `usfs_trails._TRAIL_COLUMNS`/`_BULK_SNAPSHOT_SCHEMA` - see that module's comment
# for why `geometry_wkb` (not GeoJSON text) is the on-disk column.
_TRAIL_COLUMNS = (
    "id",
    "name",
    "kind",
    "source",
    "url",
    "center_lat",
    "center_lng",
    "geometry_wkb",
    "connects",
    "length_km",
    "attrs",
)
_BULK_SNAPSHOT_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("name", pa.string()),
        ("kind", pa.string()),
        ("source", pa.string()),
        ("url", pa.string()),
        ("center_lat", pa.float64()),
        ("center_lng", pa.float64()),
        ("geometry_wkb", pa.binary()),
        ("connects", pa.string()),
        ("length_km", pa.float64()),
        ("attrs", pa.string()),
    ]
)


def _geojson_to_wkb(geojson_text: str) -> bytes:
    return shapely.to_wkb(shapely.from_geojson(geojson_text))


def _wkb_to_geojson(wkb_bytes: bytes) -> str:
    return shapely.to_geojson(shapely.from_wkb(bytes(wkb_bytes)), indent=None)


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
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind == "LineString" and coords:
        return [[(lat, lng) for lng, lat in coords]]
    if kind == "MultiLineString" and coords:
        return [[(lat, lng) for lng, lat in line] for line in coords if line]
    return []


def _sample(coords: list[tuple[float, float]], count: int) -> list[tuple[float, float]]:
    if len(coords) <= count:
        return coords
    step = (len(coords) - 1) / (count - 1)
    return [coords[round(index * step)] for index in range(count)]


def _to_lnglat(coords: list[tuple[float, float]]) -> list[list[float]]:
    return [[lng, lat] for lat, lng in coords]


def _length_km(lines: list[list[tuple[float, float]]]) -> float | None:
    """Great-circle length of the full (un-thinned) polyline set - never trust `gis_miles`/
    `seg_length`, same reasoning as `usfs_trails._length_km`."""
    total = sum(haversine_km(start[0], start[1], end[0], end[1]) for line in lines for start, end in pairwise(line))
    return round(total, 3) if total > 0 else None


# USFS road maintenance level 1 (closed/primitive, maintained only to protect the investment) -> 5
# (double-lane, paved) runs opposite to OSM's tracktype grade1 (best maintained) -> grade5
# (roughest) - same inversion `usfs_trails._tracktype` does for TRAIL_CLASS.
def _tracktype(maint_level: Any) -> str | None:
    try:
        grade = int(str(maint_level).strip().split(" ", 1)[0])
    except (TypeError, ValueError):
        return None
    if not 1 <= grade <= 5:
        return None
    return f"grade{6 - grade}"


# A road with none of the standard-vehicle classes legally open is closed to the general public's
# cars/trucks but still walkable - the `motor_vehicle`/`access`-equivalent `scoring.queries._walk_in`
# needs (TODO.md R2), synthesized here into the same OSM-vocab key so that function needs no
# source-specific branch.
_STANDARD_VEHICLE_FIELDS = ("PASSENGERVEHICLE", "HIGHCLEARANCEVEHICLE", "TRUCK")
_VEHICLE_FIELDS = (*_STANDARD_VEHICLE_FIELDS, "ATV", "MOTORCYCLE")


def _motor_vehicle(props: dict[str, Any]) -> str | None:
    # No vehicle-class field carries data at all - a real MVUM row always has at least one
    # (SYMBOL is already restricted to the System-road values that carry this matrix, see
    # `_WHERE`), so this is "nothing to derive from" rather than a legitimate closed signal.
    if not any(str(_get(props, field) or "").strip() for field in _VEHICLE_FIELDS):
        return None
    for field in _STANDARD_VEHICLE_FIELDS:
        if str(_get(props, field) or "").strip().lower() == "open":
            return None  # a standard vehicle can legally drive it - not walk-in
    return "no"


def _attrs(props: dict[str, Any]) -> dict[str, str] | None:
    """Detail attrs kept per MVUM road row. `tracktype` and `motor_vehicle` are synthesized into
    the shared OSM-derived vocab (`trails._ATTR_TAGS`) so `scoring.queries._walk_in` and the
    card's `tracktype`/rough-surface rendering (`frontend/src/map/trail-attrs.ts`) work unchanged;
    the rest are USFS-specific fields not part of that vocab, parallel to `usfs_trails._attrs`."""
    picked: dict[str, str] = {}
    maint_level = _get(props, "OPERATIONALMAINTLEVEL")
    if maint_level not in (None, ""):
        picked["road_maint_level"] = str(maint_level)
    tracktype = _tracktype(maint_level)
    if tracktype:
        picked["tracktype"] = tracktype
    motor_vehicle = _motor_vehicle(props)
    if motor_vehicle:
        picked["motor_vehicle"] = motor_vehicle
    seasonal = _get(props, "SEASONAL")
    if str(seasonal or "").strip().lower() not in ("", "yearlong"):
        picked["seasonal"] = "yes"
    ref = _get(props, "ID")
    if ref not in (None, ""):
        picked["ref"] = str(ref)
    for field, key in (
        ("SURFACETYPE", "road_surface"),
        ("JURISDICTION", "managing_org"),
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
    rte_cn = _get(props, _ID_FIELD)
    if rte_cn in (None, ""):
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
    name = _get(props, "NAME")
    if name in (None, ""):
        route_id = _get(props, "ID")
        name = f"FR {route_id}" if route_id not in (None, "") else "USFS road"
    else:
        name = str(name).strip()
    attrs = _attrs(props)
    return (
        f"usfs:road/{rte_cn}",
        name,
        "road",
        "usfs_mvum",
        _SOURCE_URL,
        center_lat,
        center_lng,
        json.dumps(trail_geometry, separators=(",", ":")),
        None,  # connects - trailhead rows only
        _length_km(lines),
        json.dumps(attrs, separators=(",", ":")) if attrs else None,
    )


class _ArcGISQueryError(RuntimeError):
    """See `usfs_trails._ArcGISQueryError` - same ArcGIS-error-as-HTTP-200 convention."""


def _iter_pages(client: httpx.Client) -> Iterator[list[dict[str, Any]]]:
    """Yield each ArcGIS response page of the whole national MVUM System-road table (no geometry
    filter - the full bulk export), paging until exhausted. See `usfs_trails._iter_pages` for the
    error-payload / short-page / `outSR` reasoning this mirrors exactly."""
    offset = 0
    while True:
        resp = client.get(
            _QUERY_URL,
            params={
                "f": "geojson",
                "where": _WHERE,
                "outFields": _out_fields(),
                "outSR": "4326",
                "returnGeometry": "true",
                "maxAllowableOffset": _SIMPLIFY_DEG,
                "resultOffset": offset,
                "resultRecordCount": _PAGE_SIZE,
            },
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise _ArcGISQueryError(f"usfs_mvum: ArcGIS query error at offset {offset}: {payload['error']}")
        if "features" not in payload:
            raise _ArcGISQueryError(f"usfs_mvum: malformed response at offset {offset} (no 'features' key)")
        features = payload["features"]
        if not features:
            return
        yield features
        offset += len(features)
        if not payload.get("exceededTransferLimit"):
            return


def stage_usfs_mvum(cfg: Settings, snapshot_date: date, run_id: str, *, client: httpx.Client | None = None) -> None:
    """Stager: pull the whole national MVUM System-road table and upload it as Parquet. Runs in
    GitHub Actions (no DB) - see `usfs_trails.stage_usfs_trails` for why a fetch failure or a
    zero-row result must not be swallowed (both propagate here identically)."""
    owns = client is None
    client = client or httpx.Client(timeout=120.0)
    by_id: dict[str, tuple[Any, ...]] = {}
    try:
        for page in _iter_pages(client):
            for feature in page:
                row = _parse_feature(feature)
                if row is not None:
                    by_id[row[0]] = row
    finally:
        if owns:
            client.close()
    if not by_id:
        raise RuntimeError("usfs_mvum: stage fetched zero rows - refusing to publish an empty snapshot")
    dict_rows = (
        dict(zip(_TRAIL_COLUMNS, (*row[:7], _geojson_to_wkb(row[7]), *row[8:]), strict=True)) for row in by_id.values()
    )
    count = spaces.write_snapshot_parquet(
        cfg.spaces, "usfs_mvum", snapshot_date, run_id, _BULK_SNAPSHOT_FILENAME, dict_rows, _BULK_SNAPSHOT_SCHEMA
    )
    logger.info("usfs_mvum: staged %d MVUM roads", count)


def load_usfs_mvum(con: psycopg.Connection, cfg: Settings, snapshot_date: date, run_id: str) -> None:
    """Loader: load the newest staged MVUM snapshot into `trails` and prune any `usfs_mvum` row
    the export no longer lists - the export is authoritative and complete, like Trail_NFS's."""
    total = 0
    ids: list[str] = []
    for batch in spaces.read_snapshot_parquet(
        cfg.spaces, "usfs_mvum", snapshot_date, run_id, _BULK_SNAPSHOT_FILENAME, batch_size=_CHUNK_SIZE
    ):
        chunk = [
            (
                rec["id"],
                rec["name"],
                rec["kind"],
                rec["source"],
                rec["url"],
                rec["center_lat"],
                rec["center_lng"],
                _wkb_to_geojson(rec["geometry_wkb"]),
                rec["connects"],
                rec["length_km"],
                rec["attrs"],
            )
            for rec in batch
        ]
        ids.extend(row[0] for row in chunk)
        if chunk:
            upsert_trails(con, chunk)
            total += len(chunk)
    pruned = cache.prune_trails_missing_from(con, "usfs_mvum", ids)
    # Namespaced under "trails:" (not "usfs_mvum:") so /healthz/data's freshness reporting (which
    # reads every `trails:`-prefixed ingest_log key) picks this load up, same as
    # usfs_trails.load_usfs_trails's own `trails:usfs:bulk:{date}` marker.
    record_ingest(con, f"trails:usfs_mvum:bulk:{snapshot_date.isoformat()}", total)
    logger.info("usfs_mvum: loaded %d MVUM roads from the bulk snapshot (pruned %d stale)", total, pruned)
