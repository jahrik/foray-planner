"""Host-tree density ingest from iNaturalist (issue #85).

Which tree genera to track is discovered from ``tree_associations`` (see
``tree_associations.py``'s GloBI ingest), not a hardcoded list. For each tracked genus, every
research-grade iNat observation within a coverage region is paged and binned into the same
lat/lng grid ``scoring.py`` uses (``cell_deg``-wide cells), aggregating to per-cell-per-genus
counts as it goes - unlike ``observations``, no raw point ever lands in the cache, so this
table stays bounded by (grid cells x tracked genera) regardless of how many tree observations
iNat has.

This is a one-time manual ingest (``foray trees``), not part of the scheduled refresh - tree
distribution doesn't change on human timescales the way fungi observations do.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Callable
from typing import Any

import psycopg

from foray.cache import is_ingested, record_ingest, tracked_host_genera, upsert_tree_density
from foray.config import CoverageRegion
from foray.inat import iter_observations, resolve_genus_taxon_id

logger = logging.getLogger(__name__)


def _coords(obs: dict[str, Any]) -> tuple[float | None, float | None]:
    """Same extraction as ingest.py's ``_coords`` - iNat observations carry coordinates either
    as GeoJSON or a legacy ``location`` string/pair, depending on endpoint/version."""
    geo = obs.get("geojson") or {}
    coords = geo.get("coordinates")
    if coords and len(coords) == 2:
        lng, lat = coords[0], coords[1]
        return float(lat), float(lng)
    loc = obs.get("location")
    if isinstance(loc, (list, tuple)) and len(loc) == 2:
        return float(loc[0]), float(loc[1])
    if isinstance(loc, str) and "," in loc:
        lat_str, lng_str = loc.split(",", 1)
        return float(lat_str), float(lng_str)
    return None, None


def _region_id(lat: float, lng: float, cell_deg: float) -> str:
    """Same grid convention as scoring.py's ``_BINNED``: floor(coord / cell_deg)."""
    ilat = math.floor(lat / cell_deg)
    ilng = math.floor(lng / cell_deg)
    return f"{ilat}_{ilng}"


def fetch_tree_density(
    *,
    region: CoverageRegion,
    cell_deg: float,
    host_genera: list[str],
    d1: str = "2000-01-01",
    d2: str | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> list[tuple[str, str, float, float, int]]:
    """Walk every research-grade observation of each ``host_genera`` genus within ``region``
    (place_id-scoped, country-level like ``ingest.ingest_region`` - not per-state tiling; iNat's
    observations endpoint already handles a whole-country paginated walk at this project's
    proven fungi-kingdom scale). Aggregates directly into (region_id, genus) counts, tracking a
    running lat/lng sum per cell so the stored center is the mean of real observation points
    (matching scoring.py's ``_CENTER_LAT``/``_CENTER_LNG`` convention) rather than the cell's
    arithmetic midpoint - no raw point is ever returned or stored.

    A host genus with no resolvable iNat taxon_id is skipped (logged), not fatal - GloBI host
    genus names occasionally don't have a clean genus-rank match on iNat.
    """
    end_date = d2 or dt.date.today().isoformat()
    counts: dict[tuple[str, str], int] = {}
    lat_sums: dict[tuple[str, str], float] = {}
    lng_sums: dict[tuple[str, str], float] = {}

    for index, genus_name in enumerate(host_genera, start=1):
        if progress_cb:
            progress_cb(
                f"Fetching {genus_name} observations in {region.name}… ({index}/{len(host_genera)})",
                (index / len(host_genera)) * 100.0,
            )
        taxon_id = resolve_genus_taxon_id(genus_name)
        if taxon_id is None:
            logger.warning("trees: could not resolve an iNat taxon_id for host genus %r - skipping", genus_name)
            continue
        for obs in iter_observations(taxon_id=taxon_id, place_id=region.place_id, d1=d1, d2=end_date):
            lat, lng = _coords(obs)
            if lat is None or lng is None:
                continue
            key = (_region_id(lat, lng, cell_deg), genus_name)
            counts[key] = counts.get(key, 0) + 1
            lat_sums[key] = lat_sums.get(key, 0.0) + lat
            lng_sums[key] = lng_sums.get(key, 0.0) + lng

    return [
        (region_id, genus_name, lat_sums[key] / cnt, lng_sums[key] / cnt, cnt)
        for key, cnt in counts.items()
        for region_id, genus_name in [key]
    ]


def ingest_trees_region(
    region: CoverageRegion,
    cell_deg: float,
    con: psycopg.Connection,
    *,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """One-time per-region host-tree density ingest. Manual only (see ``cli.py``'s ``trees``
    command) - never wired into refresh/``_VALID_REFRESH_TARGETS``. Skips if already ingested
    for this region (one-shot, like other place_id-scoped ingests)."""
    key = f"trees:place:{region.place_id}"
    if is_ingested(con, key):
        logger.info("trees: %s already ingested, skipping", region.name)
        return 0
    host_genera = tracked_host_genera(con)
    if not host_genera:
        logger.warning("trees: tree_associations is empty - run `foray tree-associations-refresh` first")
        return 0
    rows = fetch_tree_density(region=region, cell_deg=cell_deg, host_genera=host_genera, progress_cb=progress_cb)
    upserted = upsert_tree_density(con, rows)
    record_ingest(con, key, upserted)
    logger.info("trees: cached %d region/genus density rows for %s", upserted, region.name)
    return upserted
