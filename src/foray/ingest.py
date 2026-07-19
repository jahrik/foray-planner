"""Incremental ingestion: iNat observations -> Postgres cache.

Ingests every Fungi observation in one query (issue #79 Phase 4: replaced the old
per-genus loop over a fixed 21-species seed list) and resolves each observation's own
genus taxon_id from its ancestry, so phenology curves are per actual genus rather than a
fixed target. Idempotent: the observations table ignores ids already present, and each
(geo, window) pull is recorded in ingest_log.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable
from typing import Any

import psycopg

from foray.cache import (
    known_genus_taxon_ids,
    latest_obs_date,
    latest_obs_date_by_place,
    record_ingest,
    upsert_observations,
)
from foray.config import Config, CoverageRegion
from foray.inat import FUNGI_TAXON_ID, iter_observations

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 5000

# Heuristic denominator for progress reporting during the single whole-Fungi query - there's
# no cheap way to know the true row count upfront (that would need a separate iNat count call
# per window), so progress climbs toward (but never claims) 100% as rows are consumed.
_PROGRESS_ROWS_ESTIMATE = 200_000


def _coords(obs: dict[str, Any]) -> tuple[float | None, float | None]:
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


def _observed_date(obs: dict[str, Any]) -> dt.date | None:
    val = obs.get("observed_on")
    if isinstance(val, dt.datetime):
        return val.date()
    if isinstance(val, dt.date):
        return val
    if isinstance(val, str) and val:
        try:
            return dt.date.fromisoformat(val[:10])
        except ValueError:
            return None
    return None


def _resolve_genus_taxon_id(obs: dict[str, Any], known_genus_ids: set[int]) -> int | None:
    """Resolve the genus-rank taxon_id an observation belongs to.

    ``taxon.ancestor_ids`` is a flat kingdom->self int list (verified live against
    ``/v1/observations``, 2026-07-19) - no extra per-observation API call needed. Falls back
    to the observation's own taxon id when it's already genus-rank; returns ``None`` when no
    ancestor matches a known catalog genus (subfamily-rank-or-coarser IDs - see the ~110/2.67M
    count noted when this was designed).
    """
    taxon = obs.get("taxon") or {}
    if taxon.get("rank") == "genus":
        return taxon.get("id")
    for ancestor_id in taxon.get("ancestor_ids") or []:
        if ancestor_id in known_genus_ids:
            return ancestor_id
    return None


def _load_known_genus_ids(db: psycopg.Connection) -> set[int]:
    """The genus-ancestry resolver's membership set - fails fast on an empty catalog rather
    than silently ingesting only already-genus-rank observations and skipping every finer-rank
    one (a misconfigured/never-refreshed fungi_genera would otherwise look like a working but
    quietly-partial ingest)."""
    known_genus_ids = known_genus_taxon_ids(db)
    if not known_genus_ids:
        raise RuntimeError("fungi_genera catalog is empty - run `foray genera-refresh` first.")
    return known_genus_ids


def _to_row(obs: dict[str, Any], genus_taxon_id: int) -> tuple[Any, ...] | None:
    lat, lng = _coords(obs)
    day = _observed_date(obs)
    if lat is None or lng is None or day is None:
        return None
    return (
        obs["id"],
        genus_taxon_id,
        lat,
        lng,
        day,
        day.month,
        day.year,
        obs.get("quality_grade"),
        obs.get("positional_accuracy"),
        obs.get("place_guess"),
        obs.get("uri"),
        obs.get("obscured"),
    )


def ingest(
    cfg: Config,
    db: psycopg.Connection,
    progress_cb: Callable[[str, float], None] | None = None,
    abort_event: threading.Event | None = None,
) -> dict[int, int]:
    """Pull every Fungi observation within the home radius. Returns {genus_taxon_id: rows}."""
    known_genus_ids = _load_known_genus_ids(db)
    start_date = f"{cfg.since_year}-01-01"
    end_date = dt.date.today().isoformat()
    home = cfg.home

    # A country-level ingest_region() run (the nightly --countries cron, or a one-time bulk
    # load) already covers the whole country the home radius sits in - a place-scoped key has
    # no lat/lng, so latest_obs_date()'s radius check can never see it on its own. Fold in
    # that coverage too, or every live Refresh keeps re-pulling history that's already
    # sitting in Postgres (issue #141). Only safe when exactly one country is configured -
    # there's no lat/lng-to-country containment check, so with multiple countries a more
    # recently ingested *other* country could wrongly advance window_start for a home that
    # isn't even in it.
    latest = latest_obs_date(db, "fungi", home.lat, home.lng, home.radius_km)
    if len(cfg.countries) == 1:
        country_latest = latest_obs_date_by_place(db, "fungi", cfg.countries[0].place_id)
        if country_latest and (latest is None or country_latest > latest):
            latest = country_latest
    if latest:
        overlap = (dt.date.fromisoformat(latest) - dt.timedelta(days=7)).isoformat()
        window_start = max(start_date, overlap)
    else:
        window_start = start_date

    logger.info(
        "ingest: Fungi kingdom within %.0f km of %s (%s..%s)",
        home.radius_km,
        home.name,
        window_start,
        end_date,
    )

    counts: dict[int, int] = {}
    scanned = 0
    skipped_no_genus = 0
    cancelled = False
    chunk: list[tuple[Any, ...]] = []
    for obs in iter_observations(
        taxon_id=FUNGI_TAXON_ID,
        lat=home.lat,
        lng=home.lng,
        radius_km=home.radius_km,
        d1=window_start,
        d2=end_date,
        quality_grade=cfg.quality_grade,
    ):
        if abort_event and abort_event.is_set():
            logger.info("ingest: cancelled at %d observations", scanned)
            cancelled = True
            break
        scanned += 1
        if progress_cb and scanned % _CHUNK_SIZE == 0:
            progress_cb(
                f"Fetching Fungi observations… ({scanned:,} so far)",
                min(90.0, scanned / _PROGRESS_ROWS_ESTIMATE * 90.0),
            )
        genus_taxon_id = _resolve_genus_taxon_id(obs, known_genus_ids)
        if genus_taxon_id is None:
            skipped_no_genus += 1
            continue
        row = _to_row(obs, genus_taxon_id)
        if row is None:
            continue
        chunk.append(row)
        counts[genus_taxon_id] = counts.get(genus_taxon_id, 0) + 1
        if len(chunk) >= _CHUNK_SIZE:
            upsert_observations(db, chunk)
            chunk = []

    if chunk:
        upsert_observations(db, chunk)

    if cancelled:
        # A partial run must not advance the incremental cursor - record_ingest would mark
        # this whole window as covered through end_date, so a later run's latest_obs_date()
        # would skip the gap this run never actually fetched. The rows already upserted above
        # are kept (idempotent, still real data); only the "this window is done" bookkeeping
        # is skipped.
        logger.info("ingest: skipping record_ingest for cancelled run")
    else:
        key = f"obs:fungi:{home.lat}:{home.lng}:{home.radius_km}:{window_start}:{end_date}"
        record_ingest(db, key, sum(counts.values()), lat=home.lat, lng=home.lng, radius_km=home.radius_km)
    logger.info(
        "ingest: done - %d observations across %d genera (%d skipped, no genus ancestor)",
        sum(counts.values()),
        len(counts),
        skipped_no_genus,
    )
    return counts


def ingest_region(
    cfg: Config,
    db: psycopg.Connection,
    region: CoverageRegion,
    progress_cb: Callable[[str, float], None] | None = None,
    abort_event: threading.Event | None = None,
) -> dict[int, int]:
    """Pull every Fungi observation within a coverage region. Returns {genus_taxon_id: rows}.

    Bounded to the last ``cfg.region_sync_days`` regardless of whether this is the region's
    first run - full historical coverage is a one-time bulk load, not this path (see
    scripts/inat_dwca_filter.py + load_inat_bulk.py). Falling back to a full since_year
    backfill on first run is what repeatedly crashed the droplet with ENOSPC before this was
    capped.
    """
    known_genus_ids = _load_known_genus_ids(db)
    recent_cutoff = (dt.date.today() - dt.timedelta(days=cfg.region_sync_days)).isoformat()
    end_date = dt.date.today().isoformat()

    latest = latest_obs_date_by_place(db, "fungi", region.place_id)
    if latest:
        overlap = (dt.date.fromisoformat(latest) - dt.timedelta(days=7)).isoformat()
        window_start = max(recent_cutoff, overlap)
    else:
        window_start = recent_cutoff

    logger.info(
        "ingest_region: Fungi kingdom in %s (place_id=%d) %s..%s",
        region.name,
        region.place_id,
        window_start,
        end_date,
    )

    counts: dict[int, int] = {}
    scanned = 0
    skipped_no_genus = 0
    cancelled = False
    chunk: list[tuple[Any, ...]] = []
    for obs in iter_observations(
        taxon_id=FUNGI_TAXON_ID,
        place_id=region.place_id,
        d1=window_start,
        d2=end_date,
        quality_grade=cfg.quality_grade,
    ):
        if abort_event and abort_event.is_set():
            logger.info("ingest_region: cancelled at %d observations", scanned)
            cancelled = True
            break
        scanned += 1
        if progress_cb and scanned % _CHUNK_SIZE == 0:
            progress_cb(
                f"Fetching Fungi observations ({region.name})… ({scanned:,} so far)",
                min(90.0, scanned / _PROGRESS_ROWS_ESTIMATE * 90.0),
            )
        genus_taxon_id = _resolve_genus_taxon_id(obs, known_genus_ids)
        if genus_taxon_id is None:
            skipped_no_genus += 1
            continue
        row = _to_row(obs, genus_taxon_id)
        if row is None:
            continue
        chunk.append(row)
        counts[genus_taxon_id] = counts.get(genus_taxon_id, 0) + 1
        if len(chunk) >= _CHUNK_SIZE:
            upsert_observations(db, chunk)
            chunk = []

    if chunk:
        upsert_observations(db, chunk)

    if cancelled:
        # See ingest()'s matching comment: don't advance the incremental cursor on a partial run.
        logger.info("ingest_region: skipping record_ingest for cancelled run")
    else:
        key = f"obs:fungi:place:{region.place_id}:{window_start}:{end_date}"
        record_ingest(db, key, sum(counts.values()))
    logger.info(
        "ingest_region: done %s - %d observations across %d genera (%d skipped, no genus ancestor)",
        region.name,
        sum(counts.values()),
        len(counts),
        skipped_no_genus,
    )
    return counts
