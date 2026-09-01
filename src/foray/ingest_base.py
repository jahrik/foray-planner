"""Shared skeleton for the home-radius "area" ingests (campgrounds, dispersed camping,
public land, trails).

Each of those was the same ~20 lines: open/borrow a connection, skip if the home disk is
already covered, fetch, upsert, record the ingest, log. Only the source (its fetch function
and target table) and a few log/progress strings differ. ``run_area_ingest`` captures the
skeleton so a new area source - or a new one for rain #226 / fire #227 - is just a fetch
function plus an upsert function.

The coverage-wide / per-region variants (``land.ingest_public_land_coverage``,
``trails.ingest_trails_region``) are keyed on ``is_ingested`` rather than a lat/lng disk and
stay in their own modules.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import psycopg

from foray import cache
from foray.config import Settings

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str, float], None]
AreaFetch = Callable[..., list[tuple[Any, ...]]]
Upsert = Callable[[psycopg.Connection, Sequence[tuple[Any, ...]]], int]


def run_area_ingest(
    cfg: Settings,
    con: psycopg.Connection | None,
    *,
    prefix: str,
    label: str,
    noun: str,
    fetch: AreaFetch,
    upsert: Upsert,
    progress_cb: ProgressCb | None = None,
) -> int:
    """Ingest one best-effort area source for the home disk. Returns rows upserted.

    - ``prefix``: the ``ingest_log`` key prefix, also the ``is_area_covered`` skip prefix
      (e.g. ``"camps:ridb:"``, ``"trails:"``).
    - ``label``: short log tag (e.g. ``"camps"``).
    - ``noun``: subject for the "already cached" progress message (e.g. ``"Campgrounds"``).
    - ``fetch``: called as ``fetch(lat=, lng=, radius_km=, progress_cb=)`` - the caller binds
      the httpx client and any source-specific kwargs (an API key, a source list) first.
    - ``upsert``: the matching ``cache.upsert_*`` for the rows ``fetch`` returns.
    """
    home = cfg.home
    with cache.connection(con) as db:
        if cache.is_area_covered(db, prefix, home.lat, home.lng, home.radius_km):
            logger.info("%s: already ingested for this area, skipping", label)
            if progress_cb:
                progress_cb(f"{noun} already cached, skipping…", 100.0)
            return 0
        logger.info("%s: fetching within %.0f km of home…", label, home.radius_km)
        rows = fetch(lat=home.lat, lng=home.lng, radius_km=home.radius_km, progress_cb=progress_cb)
        upsert(db, rows)
        key = f"{prefix}{home.lat}:{home.lng}:{home.radius_km}"
        cache.record_ingest(db, key, len(rows), lat=home.lat, lng=home.lng, radius_km=home.radius_km)
        logger.info("%s: cached %d rows for this area", label, len(rows))
        return len(rows)
