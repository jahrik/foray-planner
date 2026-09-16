"""Per-region-grid-cell caches that never need re-resolving once set: the reverse-
geocoded place name (issue #206) and the satellite-imagery fill (#293 follow-up).
Also the phenology-rebuild debounce trigger (issue #332 PR 2), which lives here rather
than in ``foray.scoring.regions`` to avoid a cache<->scoring import cycle."""

from __future__ import annotations

import httpx
import psycopg

from foray import spaces
from foray.config import Settings


def maybe_rebuild_phenology(con: psycopg.Connection, cfg: Settings, new_rows: int) -> bool:
    """Debounced phenology rebuild (issue #332 PR 2). Every enrichment pass (ingest,
    revalidate, elevation/precip backfill) used to call `scoring.regions.build_phenology`
    inline whenever it changed anything - the heaviest single op in the app, run far more
    often than the data actually needed it. This accumulates `new_rows` into a `meta`-stored
    counter and only actually rebuilds once the running total crosses
    `cfg.observability.phenology_rebuild_threshold` - the counter is reset only *after* a
    successful rebuild, so a failed rebuild (exception propagates to the caller) leaves the
    accumulated count in place for the next pass to retry, instead of silently forgetting it.

    A session advisory lock (distinct from the per-job lock `foray.jobs` already takes - two
    *different* jobs can each push the counter past the threshold in the same tick) serializes
    the check against the counter; `build_phenology` itself takes the same lock again (safe -
    session-level advisory locks are reentrant) so the rebuild also can't race a direct
    `build_phenology` call from `run_home_refresh` or the coverage-wide `refresh --all` CLI
    path. Returns whether a rebuild actually ran.
    """
    if new_rows <= 0:
        return False
    from foray.scoring.regions import build_phenology  # local: avoid a cache<->scoring import cycle

    con.execute("SELECT pg_advisory_lock(hashtext('phenology-rebuild'))")
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'phenology_pending_rows'").fetchone()
        pending = (int(row[0]) if row else 0) + new_rows
        # Persist the accumulated count *before* attempting a rebuild - if build_phenology
        # raises, this value (not the pre-call one, and not 0) is what's on the next attempt,
        # so nothing is lost and the threshold isn't re-earned from scratch.
        con.execute(
            "INSERT INTO meta (key, value) VALUES ('phenology_pending_rows', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [str(pending)],
        )
        if pending < cfg.observability.phenology_rebuild_threshold:
            return False
        build_phenology(con, cfg.h3_resolution)
        con.execute(
            "INSERT INTO meta (key, value) VALUES ('phenology_pending_rows', '0') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
        return True
    finally:
        con.execute("SELECT pg_advisory_unlock(hashtext('phenology-rebuild'))")


def load_region_place(con: psycopg.Connection, region_id: str) -> tuple[bool, str | None]:
    """Cached place name for a region, if already resolved. ``(found, place_name)`` -
    ``found=False`` means no lookup has been attempted yet (caller should resolve and save);
    ``found=True, place_name=None`` means a lookup ran and found nothing notable nearby."""
    row = con.execute("SELECT place_name FROM region_places WHERE region_id = %s", [region_id]).fetchone()
    if row is None:
        return False, None
    return True, row[0]


def load_region_places(con: psycopg.Connection, region_ids: list[str]) -> dict[str, str | None]:
    """Batch form of ``load_region_place`` for a whole result page's card titles (issue #301).
    Returns one entry per region that has a cached lookup (``place_name`` may be ``None`` - a
    lookup that ran and found nothing notable); regions with no attempt yet are simply absent,
    so ``region_id in result`` is the ``found`` flag."""
    if not region_ids:
        return {}
    rows = con.execute(
        "SELECT region_id, place_name FROM region_places WHERE region_id = ANY(%s)",
        [region_ids],
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def save_region_place(con: psycopg.Connection, region_id: str, place_name: str | None) -> None:
    con.execute(
        "INSERT INTO region_places (region_id, place_name) VALUES (%s, %s) ON CONFLICT (region_id) DO NOTHING",
        [region_id, place_name],
    )


def load_region_satellite(con: psycopg.Connection, cfg: Settings, region_id: str) -> tuple[bytes, bytes] | None:
    """Cached ``(image, labels)`` bytes for a region's satellite fill, or ``None`` if never
    fetched - caller should fetch (``sources.satellite.fetch_region_satellite``) and save.

    Reads whichever storage `save_region_satellite` wrote to: a Space URL (fetched over HTTP)
    when `cfg.spaces` is configured, the row's own bytea columns otherwise. A region cached
    under one mode before a `spaces` config change still reads back correctly - both columns are
    checked regardless of the current config."""
    row = con.execute(
        "SELECT image, labels, image_url, labels_url FROM region_satellite WHERE region_id = %s", [region_id]
    ).fetchone()
    if row is None:
        return None
    image, labels, image_url, labels_url = row
    if image_url is not None and labels_url is not None:
        with httpx.Client(timeout=30) as client:
            image_response, labels_response = client.get(image_url), client.get(labels_url)
        image_response.raise_for_status()
        labels_response.raise_for_status()
        return image_response.content, labels_response.content
    if image is not None and labels is not None:
        return bytes(image), bytes(labels)
    return None


def save_region_satellite(con: psycopg.Connection, cfg: Settings, region_id: str, image: bytes, labels: bytes) -> None:
    if cfg.spaces.configured:
        image_url = spaces.put_object(cfg.spaces, f"satellite/{region_id}/image.jpg", image, "image/jpeg", public=True)
        labels_url = spaces.put_object(
            cfg.spaces, f"satellite/{region_id}/labels.png", labels, "image/png", public=True
        )
        con.execute(
            "INSERT INTO region_satellite (region_id, image_url, labels_url) VALUES (%s, %s, %s) "
            "ON CONFLICT (region_id) DO NOTHING",
            [region_id, image_url, labels_url],
        )
    else:
        con.execute(
            "INSERT INTO region_satellite (region_id, image, labels) VALUES (%s, %s, %s) "
            "ON CONFLICT (region_id) DO NOTHING",
            [region_id, image, labels],
        )
