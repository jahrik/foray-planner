"""Incremental ingestion: iNat observations -> Postgres cache.

Ingests one seed taxon at a time and tags every observation with that *seed* taxon_id
(not the leaf species), so phenology curves are per foraging target. Idempotent: the
observations table ignores ids already present, and each (taxon, geo, window) pull is
recorded in ingest_log.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable
from typing import Any

import psycopg

from foray.cache import (
    latest_obs_date,
    latest_obs_date_by_place,
    record_ingest,
    upsert_observations,
    upsert_taxa,
)
from foray.config import Config, CoverageRegion
from foray.inat import iter_observations

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 5000


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


def _to_row(obs: dict[str, Any], seed_taxon_id: int) -> tuple[Any, ...] | None:
    lat, lng = _coords(obs)
    day = _observed_date(obs)
    if lat is None or lng is None or day is None:
        return None
    return (
        obs["id"],
        seed_taxon_id,
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
    """Pull observations for every seed taxon within the home radius. Returns {taxon_id: rows}."""
    upsert_taxa(
        db,
        [
            {
                "taxon_id": species.taxon_id,
                "name": species.name,
                "common_name": species.common_name,
                "rank": species.rank,
            }
            for species in cfg.species
        ],
    )

    start_date = f"{cfg.since_year}-01-01"
    end_date = dt.date.today().isoformat()
    home = cfg.home
    counts: dict[int, int] = {}

    total = len(cfg.species)
    logger.info(
        "ingest: %d taxa within %.0f km of %s (%s..%s)",
        total,
        home.radius_km,
        home.name,
        start_date,
        end_date,
    )
    for index, species in enumerate(cfg.species, start=1):
        if progress_cb:
            progress_cb(f"Fetching {species.common_name}…", (index - 1) / total * 100.0)
        latest = latest_obs_date(db, species.taxon_id, home.lat, home.lng, home.radius_km)
        if latest:
            overlap = (dt.date.fromisoformat(latest) - dt.timedelta(days=7)).isoformat()
            species_start = max(start_date, overlap)
        else:
            species_start = start_date

        # Removing the skip-if-latest==end_date logic since we now overlap by 7 days

        logger.info(
            "ingest [%d/%d] %s (taxon %d) from %s…",
            index,
            total,
            species.common_name,
            species.taxon_id,
            species_start,
        )
        rows: list[tuple[Any, ...]] = []
        for obs in iter_observations(
            taxon_id=species.taxon_id,
            lat=home.lat,
            lng=home.lng,
            radius_km=home.radius_km,
            d1=species_start,
            d2=end_date,
            quality_grade=cfg.quality_grade,
        ):
            if abort_event and abort_event.is_set():
                break
            row = _to_row(obs, species.taxon_id)
            if row is not None:
                rows.append(row)

        if abort_event and abort_event.is_set():
            logger.info("ingest: cancelled during %s", species.common_name)
            break
        upsert_observations(db, rows)
        key = f"obs:{species.taxon_id}:{home.lat}:{home.lng}:{home.radius_km}:{species_start}:{end_date}"
        record_ingest(db, key, len(rows), lat=home.lat, lng=home.lng, radius_km=home.radius_km)
        counts[species.taxon_id] = len(rows)
        logger.info("ingest [%d/%d] %s: %d observations", index, total, species.common_name, len(rows))

    logger.info("ingest: done - %d observations across %d taxa", sum(counts.values()), total)
    return counts


def ingest_region(
    cfg: Config,
    db: psycopg.Connection,
    region: CoverageRegion,
    progress_cb: Callable[[str, float], None] | None = None,
    abort_event: threading.Event | None = None,
) -> dict[int, int]:
    """Pull observations for every seed taxon within a coverage region. Returns {taxon_id: rows}."""
    upsert_taxa(
        db,
        [
            {
                "taxon_id": species.taxon_id,
                "name": species.name,
                "common_name": species.common_name,
                "rank": species.rank,
            }
            for species in cfg.species
        ],
    )

    start_date = f"{cfg.since_year}-01-01"
    end_date = dt.date.today().isoformat()
    counts: dict[int, int] = {}

    total = len(cfg.species)
    logger.info(
        "ingest_region: %d taxa in %s (place_id=%d) %s..%s",
        total,
        region.name,
        region.place_id,
        start_date,
        end_date,
    )
    for index, species in enumerate(cfg.species, start=1):
        if progress_cb:
            progress_cb(f"Fetching {species.common_name} ({region.name})…", (index - 1) / total * 100.0)
        latest = latest_obs_date_by_place(db, species.taxon_id, region.place_id)
        if latest:
            overlap = (dt.date.fromisoformat(latest) - dt.timedelta(days=7)).isoformat()
            species_start = max(start_date, overlap)
        else:
            species_start = start_date

        logger.info(
            "ingest_region [%d/%d] %s (taxon %d) in %s from %s…",
            index,
            total,
            species.common_name,
            species.taxon_id,
            region.name,
            species_start,
        )
        chunk: list[tuple[Any, ...]] = []
        total_rows = 0
        for obs in iter_observations(
            taxon_id=species.taxon_id,
            place_id=region.place_id,
            d1=species_start,
            d2=end_date,
            quality_grade=cfg.quality_grade,
        ):
            if abort_event and abort_event.is_set():
                break
            row = _to_row(obs, species.taxon_id)
            if row is not None:
                chunk.append(row)
                if len(chunk) >= _CHUNK_SIZE:
                    upsert_observations(db, chunk)
                    total_rows += len(chunk)
                    chunk = []

        if abort_event and abort_event.is_set():
            logger.info("ingest_region: cancelled during %s", species.common_name)
            break
        if chunk:
            upsert_observations(db, chunk)
            total_rows += len(chunk)
        key = f"obs:{species.taxon_id}:place:{region.place_id}:{species_start}:{end_date}"
        record_ingest(db, key, total_rows)
        counts[species.taxon_id] = total_rows
        logger.info(
            "ingest_region [%d/%d] %s: %d observations",
            index,
            total,
            species.common_name,
            total_rows,
        )

    logger.info(
        "ingest_region: done %s - %d observations across %d taxa",
        region.name,
        sum(counts.values()),
        total,
    )
    return counts
