"""RAVG burn-severity bulk source (issue #335 PR 4 / TODO.md R6).

Fills the ~1.5-2 yr gap MTBS's own severity classification leaves: dominant_severity has
stayed NULL for recent fires. The original #227 plan pointed ``refresh_fire`` at
``portal.mtbs.gov``'s MTBS_ATBI severity service - confirmed 2026-09-14 that host no longer
even resolves, so this had been failing silently on every refresh (caught by the broad
``_SOURCE_ERRORS`` except in ``fire.py``, logged as a warning) since #227 shipped.

MTBS's own live-confirmed national boundary layer (``EDW_MTBS_01/MapServer/63``, checked
2026-09-14) isn't the fix - it carries fire_id/name/year/acres and dNBR thresholds, not a
per-severity-class acreage breakdown. That data was apparently only ever exposed by the now-dead
portal.mtbs.gov service. RAVG is the confirmed-live replacement: it publishes within ~45 days of
containment (well inside MTBS's own multi-year lag), via ``EDW_RAVG_v2_01/MapServer/0`` - found
through an ArcGIS Online org search after every guessed name under ``apps.fs.usda.gov``'s
``RDW_Wildfire`` folder 403'd (matching this issue's own "listing 403'd" risk note; the folder
itself 403s too, only individual confirmed service URLs work).

RAVG's schema is a vegetation-mortality proxy, not MTBS's unburned/low/moderate/high acreage
split: ``tree_acres`` (forested acres RAVG actually assessed within the fire), ``tree_ac_50`` /
``tree_ac_75`` (acres with >=50%/>=75% basal-area loss). Mapped onto the shape
``cache.apply_fire_severity`` already expects: ``high`` = tree_ac_75, ``moderate`` = tree_ac_50 -
tree_ac_75, ``low`` = tree_acres - tree_ac_50, ``unburned`` = the fire's total ``acres`` outside
the assessed forested footprint. An approximation, not MTBS's own classification - the only
nationally-confirmed source for this signal, and ``dominant_severity`` only ever gates a binary
"boostable or not" check in ``ranking._BOOSTABLE_SEVERITY`` (high excluded, everything else
boosted), so the exact acreage split matters less than getting the high/not-high call right.

Match key: checked live whether RAVG's ``event_id`` (e.g. ``OR4345912273220240718``) lines up
with anything in our WFIGS-derived ``fire_perimeters`` rows - it doesn't. Our ``irwin_id`` column
is WFIGS's ``IRWINID``, a GUID (``{30ED7D11-...}``); WFIGS also separately carries a compact
``UNQE_FIRE_ID`` (``2024-AZPNF-001457``) - neither matches RAVG's format, and no shared id field
was found. Falls back to a normalized name+year match (``cache.apply_fire_severity``'s
``"name_year"`` key) - best-effort, like the rest of this layer.

Bulk via ``ingest-bulk`` (#357's codified standard), not a live droplet fetch - same stage/load
split as ``usfs_trails.py``. No geometry column: this source only ever enriches existing
``fire_perimeters`` rows, it never creates new ones.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import psycopg
import pyarrow as pa

from foray import spaces
from foray.cache import apply_fire_severity, record_ingest
from foray.config import Settings
from foray.sources.http import USER_AGENT

logger = logging.getLogger(__name__)

_QUERY_URL = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_RAVG_v2_01/MapServer/0/query"
_PAGE_SIZE = 1000
_FIELDS = ("event_id", "fire_name", "fire_year", "acres", "tree_acres", "tree_ac_50", "tree_ac_75")

_BULK_SNAPSHOT_FILENAME = "ravg_severity.parquet"
_BULK_SNAPSHOT_SCHEMA = pa.schema(
    [
        ("event_id", pa.string()),
        ("fire_name", pa.string()),
        ("fire_year", pa.int32()),
        ("acres", pa.float64()),
        ("tree_acres", pa.float64()),
        ("tree_ac_50", pa.float64()),
        ("tree_ac_75", pa.float64()),
    ]
)

_FIRE_NAME_SUFFIX = re.compile(r"\s+FIRE$", re.IGNORECASE)


def normalize_fire_name(name: str) -> str:
    """Uppercase + strip a trailing " Fire" - RAVG's `fire_name` and WFIGS's incident name
    disagree on that suffix often enough (checked live: "No Man" vs "No Man Fire"-style pairs)
    that a literal comparison would miss real matches."""
    return _FIRE_NAME_SUFFIX.sub("", name.strip()).upper()


class _ArcGISQueryError(RuntimeError):
    """Mirrors `usfs_trails._ArcGISQueryError` - an ArcGIS error payload or malformed response
    must not look like a clean end-of-pagination, or a partial fetch could publish silently."""


def _iter_pages(client: httpx.Client) -> Iterator[list[dict[str, Any]]]:
    """Page the whole national RAVG PostfireVegChg table (no geometry, no filter - a live count
    against the real service, checked 2026-09-14: 1,407 features)."""
    offset = 0
    while True:
        resp = client.get(
            _QUERY_URL,
            params={
                "f": "json",
                "where": "1=1",
                "outFields": ",".join(_FIELDS),
                "returnGeometry": "false",
                "resultOffset": offset,
                "resultRecordCount": _PAGE_SIZE,
            },
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise _ArcGISQueryError(f"ravg: ArcGIS query error at offset {offset}: {payload['error']}")
        if "features" not in payload:
            raise _ArcGISQueryError(f"ravg: malformed response at offset {offset} (no 'features' key)")
        features = payload["features"]
        if not features:
            return
        yield features
        offset += len(features)
        if not payload.get("exceededTransferLimit"):
            return


def _parse_feature(feature: dict[str, Any]) -> dict[str, Any] | None:
    attrs = feature.get("attributes") or {}
    fire_name = attrs.get("fire_name")
    fire_year_raw = attrs.get("fire_year")
    if not fire_name or fire_year_raw in (None, ""):
        return None
    try:
        fire_year = int(fire_year_raw)
    except (TypeError, ValueError):
        return None
    tree_acres = attrs.get("tree_acres")
    if tree_acres in (None, 0):
        # No forested acres assessed - RAVG has nothing to say about this fire's severity.
        return None
    return {
        "event_id": attrs.get("event_id"),
        "fire_name": str(fire_name).strip(),
        "fire_year": fire_year,
        "acres": attrs.get("acres"),
        "tree_acres": tree_acres,
        "tree_ac_50": attrs.get("tree_ac_50") or 0.0,
        "tree_ac_75": attrs.get("tree_ac_75") or 0.0,
    }


def stage_ravg(cfg: Settings, snapshot_date: date, run_id: str, *, client: httpx.Client | None = None) -> None:
    """Stager: pull the whole national RAVG PostfireVegChg table and upload as Parquet. Runs in
    GitHub Actions, not the droplet (#357). Refuses to publish a zero-row result - this source is
    never legitimately empty."""
    owns = client is None
    client = client or httpx.Client(timeout=60.0)
    rows: list[dict[str, Any]] = []
    try:
        for page in _iter_pages(client):
            for feature in page:
                row = _parse_feature(feature)
                if row is not None:
                    rows.append(row)
    finally:
        if owns:
            client.close()
    if not rows:
        raise RuntimeError("ravg: stage fetched zero rows - refusing to publish an empty snapshot")
    count = spaces.write_snapshot_parquet(
        cfg.spaces, "ravg", snapshot_date, run_id, _BULK_SNAPSHOT_FILENAME, rows, _BULK_SNAPSHOT_SCHEMA
    )
    logger.info("ravg: staged %d fires with a RAVG severity assessment", count)


def load_ravg(con: psycopg.Connection, cfg: Settings, snapshot_date: date, run_id: str) -> None:
    """Loader: map each staged RAVG row onto ``cache.apply_fire_severity``'s tuple shape and
    apply it to whatever ``fire_perimeters`` rows already exist (WFIGS-sourced) - this source
    never creates its own perimeter rows, only enriches existing ones by normalized name+year."""
    total = 0
    for batch in spaces.read_snapshot_parquet(cfg.spaces, "ravg", snapshot_date, run_id, _BULK_SNAPSHOT_FILENAME):
        severity_rows = []
        for rec in batch:
            tree_acres, tree_50, tree_75 = rec["tree_acres"], rec["tree_ac_50"], rec["tree_ac_75"]
            low = max(tree_acres - tree_50, 0.0)
            moderate = max(tree_50 - tree_75, 0.0)
            high = tree_75
            unburned = max((rec["acres"] or tree_acres) - tree_acres, 0.0)
            classes = {"low": low, "moderate": moderate, "high": high}
            dominant = max(classes, key=lambda key: classes[key]) if any(classes.values()) else None
            match_value = (normalize_fire_name(rec["fire_name"]), rec["fire_year"])
            severity_rows.append(("name_year", match_value, unburned, low, moderate, high, dominant, None))
        total += apply_fire_severity(con, severity_rows)
    record_ingest(con, f"fire:ravg:bulk:{snapshot_date.isoformat()}", total)
    logger.info("ravg: applied severity to %d fire_perimeters rows from the bulk snapshot", total)
