"""Bulk-snapshot USFS Trail_NFS loader (issue #335 PR 3a).

OSM can't reliably give legal-access or seasonal attributes for national-forest trails; today's
proxies for that in ``trails.py`` are all OSM-derived guesses (``ref``/``operator`` heuristics,
``barrier`` nodes, the ``_walk_in`` tag derivation). The USFS Enterprise Data Warehouse publishes
``Trans_Trail_NFS_Publish`` - the authoritative national foot/stock trail network, including
trails OSM is missing entirely (e.g. Lost Man Creek Trail). This module pulls that layer only
(``EDW_TrailNFSPublish_01/MapServer/0``); the MVUM roads layer (which carries the OHV-legality /
open-season matrix) and OSM/USFS dedup are follow-up PRs - see TODO.md R2.

Issue #335 specifies this source goes "via `ingest-bulk`" (the #334 bulk-snapshot pipeline), not
a live per-request/coverage fetch on the droplet - the national feature service is queried here
(``_iter_pages``, no geometry filter - the whole ``TRAIL_TYPE='TERRA'`` table, paged), but that
query runs in the **stager** (``stage_usfs_trails``), which GitHub Actions runs on its own
schedule (``.github/workflows/bulk-load.yml``), never the 1-vCPU droplet. The **loader**
(``load_usfs_trails``) just downloads the staged snapshot and upserts it - the same
stage/load split ``inat_bulk``/``camps.stage_ridb``/``load_ridb`` already use.

Rows land in the same ``trails`` table as the OSM ingest (``source='usfs'`` keeps them distinct;
``kind='path'`` so ``trails_near``/``nearest_trail`` - which filter on ``kind``, not ``source`` -
pick them up unchanged). The export is authoritative and complete, like RIDB's, so a load also
prunes any ``usfs``-sourced trail the newest snapshot no longer lists (a decommissioned trail).
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

_QUERY_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_TrailNFSPublish_01/MapServer/0/query"
_SOURCE_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_TrailNFSPublish_01/MapServer/0"
# Terrestrial trails only - SNOW/WATER trail_type rows aren't a foraging surface.
_WHERE = "TRAIL_TYPE='TERRA'"

_PAGE_SIZE = 1000
# Matches land.py's ownership-polygon generalization; a national trail network's full vertex
# detail is unnecessary for the map and would balloon the cached geometry.
_SIMPLIFY_DEG = 0.0001
_MAX_POINTS_PER_LINE = 60
_CHUNK_SIZE = 5000

_ID_FIELD = "TRAIL_CN"
_FIELDS = (
    _ID_FIELD,
    "TRAIL_NAME",
    "TRAIL_CLASS",
    "TRAIL_SURFACE",
    "MANAGING_ORG",
    "NATIONAL_TRAIL_DESIGNATION",
)

_BULK_SNAPSHOT_FILENAME = "trails.parquet"
# Same order as `_parse_feature`'s tuple, except `geometry_wkb` (WKB bytes) replaces that
# tuple's `geojson` (GeoJSON text) element - issue #359's geometry-encoding decision, compact
# and PostGIS-native, but the `trail_geometry` table's insert trigger
# (`foray_trail_geom_from_geometry`) still wants GeoJSON text, so `load_usfs_trails` converts
# back via `_wkb_to_geojson` before calling `upsert_trails` rather than this format change
# reaching that far.
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
    total = sum(haversine_km(start[0], start[1], end[0], end[1]) for line in lines for start, end in pairwise(line))
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


class _ArcGISQueryError(RuntimeError):
    """ArcGIS returned an error payload as an HTTP 200 (its own convention for query errors -
    a bad field name, an over-budget request, ...), which `raise_for_status()` never sees since
    the transport call itself succeeded."""


def _iter_pages(client: httpx.Client) -> Iterator[list[dict[str, Any]]]:
    """Yield each ArcGIS response page (<= `_PAGE_SIZE` features) of the whole national
    ``TRAIL_TYPE='TERRA'`` table (no geometry filter - the full bulk export, like RIDB's full
    CSV), paging until exhausted. A live count against the real service (checked 2026-09-14):
    78,149 features.

    Raises `_ArcGISQueryError` on an error payload or a malformed response missing `features`
    entirely - a Copilot review catch: without this, a query failure (bad field name, service
    hiccup) looks identical to "no more pages" (an empty `features` list is the normal
    end-of-pagination signal), so it would otherwise be swallowed as if pagination just
    finished early, silently staging (and, worse, publishing) a truncated or empty snapshot.

    Keeps paging on any `exceededTransferLimit: true` response regardless of that page's
    feature count (another Copilot review catch): ArcGIS sets that flag when either the
    record-count limit (`_PAGE_SIZE`) *or* the response's transfer-size limit is hit, so a page
    can be truncated by size with fewer than `_PAGE_SIZE` features and still have more data
    waiting at the next offset - stopping on "short page" alone would silently drop the
    remainder into the same truncated-snapshot failure mode as the two checks above.

    ``outSR=4326`` is explicit (matching `land.py`'s ArcGIS calls) rather than relying on
    `f=geojson` alone implying WGS84 output - belt-and-braces for an authoritative snapshot
    whose load then prunes every trail not in it.
    """
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
            raise _ArcGISQueryError(f"usfs_trails: ArcGIS query error at offset {offset}: {payload['error']}")
        if "features" not in payload:
            raise _ArcGISQueryError(f"usfs_trails: malformed response at offset {offset} (no 'features' key)")
        features = payload["features"]
        if not features:
            return
        yield features
        offset += len(features)
        if not payload.get("exceededTransferLimit"):
            return


def stage_usfs_trails(cfg: Settings, snapshot_date: date, run_id: str, *, client: httpx.Client | None = None) -> None:
    """Stager: pull the whole national Trail_NFS foot-trail table and upload it as a Parquet file
    under this run's Space prefix. Runs in GitHub Actions (no DB) - the droplet never touches the
    live ArcGIS service (issue #335 PR 3a: this source is "via `ingest-bulk`", not a live
    per-request/coverage crawl on the 1-vCPU box). ``geometry_wkb``/``attrs`` stay as their
    already-encoded bytes/JSON-string elements, so ``load_usfs_trails`` can load them with no
    reparsing beyond the WKB->GeoJSON conversion (`_wkb_to_geojson`) `upsert_trails` needs.

    Unlike the live/best-effort area ingests, a fetch failure here is **not** swallowed - it's
    left to propagate (matching `camps.stage_ridb`/`inat_bulk.stage_inat`, neither of which
    catches anything either). `ingest_bulk.stage_snapshot` calls `spaces.publish_snapshot` right
    after this returns, with no other success signal - catching the error here (a Copilot review
    catch on the first cut of this function) would have turned a failed fetch into a published
    *partial* snapshot, which `load_usfs_trails`' prune-to-exactly-what's-listed step would then
    have read as authoritative, deleting every USFS trail the partial fetch didn't happen to
    reach. Also refuses to stage a zero-row result - this source is never legitimately empty, so
    an empty result is a signal something went wrong, not a valid snapshot.
    """
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
        raise RuntimeError("usfs_trails: stage fetched zero rows - refusing to publish an empty snapshot")
    dict_rows = (
        dict(zip(_TRAIL_COLUMNS, (*row[:7], _geojson_to_wkb(row[7]), *row[8:]), strict=True)) for row in by_id.values()
    )
    count = spaces.write_snapshot_parquet(
        cfg.spaces, "usfs_trails", snapshot_date, run_id, _BULK_SNAPSHOT_FILENAME, dict_rows, _BULK_SNAPSHOT_SCHEMA
    )
    logger.info("usfs_trails: staged %d USFS foot trails", count)


def load_usfs_trails(con: psycopg.Connection, cfg: Settings, snapshot_date: date, run_id: str) -> None:
    """Loader: load the newest staged Trail_NFS snapshot into ``trails`` and prune any ``usfs``
    row the export no longer lists (a decommissioned trail) - the export is authoritative and
    complete, like RIDB's full facility list (``camps.load_ridb``)."""
    total = 0
    ids: list[str] = []
    for batch in spaces.read_snapshot_parquet(
        cfg.spaces, "usfs_trails", snapshot_date, run_id, _BULK_SNAPSHOT_FILENAME, batch_size=_CHUNK_SIZE
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
    pruned = cache.prune_trails_missing_from(con, "usfs", ids)
    # Namespaced under "trails:" (not "usfs_trails:") so /healthz/data's freshness reporting
    # (which reads every `trails:`-prefixed ingest_log key) picks this load up, same as
    # camps.load_ridb's `camps:ridb:bulk:{date}` marker for the campgrounds layer.
    record_ingest(con, f"trails:usfs:bulk:{snapshot_date.isoformat()}", total)
    logger.info("usfs_trails: loaded %d USFS trails from the bulk snapshot (pruned %d stale)", total, pruned)
