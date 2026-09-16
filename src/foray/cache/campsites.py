"""Campsite (developed campground + OSM dispersed-site) table writes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, LiteralString

import psycopg

from foray.cache.core import _invalidate_rank_cache, upsert_rows


def upsert_campsites(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert campsite tuples, refreshing existing rows in place. Returns rows attempted.

    Each tuple is (id, name, kind, fee, free, lat, lng, source, url, reservable, fee_low, fee_high).
    """
    columns: tuple[LiteralString, ...] = (
        "id",
        "name",
        "kind",
        "fee",
        "free",
        "lat",
        "lng",
        "source",
        "url",
        "reservable",
        "fee_low",
        "fee_high",
    )
    result = upsert_rows(con, "campsites", columns, rows)
    _invalidate_rank_cache()
    return result


def prune_campsites_outside_radius(
    con: psycopg.Connection, source: str, lat: float, lng: float, radius_km: float
) -> int:
    """Delete ``source`` campsites outside ``radius_km`` of (``lat``, ``lng``). Returns rows deleted.

    Campsite ingests only ever upsert, so shrinking the home radius (or moving home) leaves the
    old wider area's rows behind forever (issue #306: 194 of 285 ridb rows were stale). A row
    outside the current disk is stale by definition regardless of whether the last fetch was
    complete, so this is safe to run unconditionally after every ingest.
    """
    result = con.execute(
        "DELETE FROM campsites WHERE source = %s "
        "AND (geom IS NULL OR NOT ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s))",
        [source, lng, lat, radius_km * 1000.0],
    )
    con.commit()
    if result.rowcount:
        # A run that only shrinks/moves the covered area deletes rows here without ever calling
        # upsert_campsites - without this, cached access scores could keep crediting a campsite
        # this exact prune just removed (Copilot review, PR #348). After commit, not before -
        # this DELETE is already committed by the time any concurrent read could recompute and
        # repopulate the cache, so there's no invalidate-before-commit race here.
        _invalidate_rank_cache()
    return result.rowcount


def prune_campsites_outside_bounds(
    con: psycopg.Connection, source: str, west: float, south: float, east: float, north: float
) -> int:
    """Delete ``source`` campsites outside the ``(west, south, east, north)`` envelope. Returns rows deleted.

    The coverage-wide camp ingests (issue #306 workstream B) upsert every facility in the
    configured coverage; this clears rows a shrunk coverage set no longer includes, the same
    way ``prune_campsites_outside_radius`` does for the home-disk path.
    """
    result = con.execute(
        "DELETE FROM campsites WHERE source = %s "
        "AND (geom IS NULL OR NOT ST_Intersects("
        "geom::geometry, ST_MakeEnvelope(%s, %s, %s, %s, 4326)))",
        [source, west, south, east, north],
    )
    con.commit()
    if result.rowcount:
        _invalidate_rank_cache()  # see prune_campsites_outside_radius
    return result.rowcount


def prune_campsites_missing_from(con: psycopg.Connection, source: str, ids: Sequence[str]) -> int:
    """Delete ``source`` campsites whose id isn't in ``ids``. Returns rows deleted.

    For a source loaded from an authoritative full dump (the RIDB bulk snapshot, issue #334
    PR 2) every row it lists is the complete truth, unlike the home-radius/coverage-envelope
    prunes above - a facility the dump no longer lists (closed, delisted, merged into another
    id) has to go regardless of where it sits geographically. Deletes nothing if ``ids`` is
    empty - an empty load is far more likely a bug than a real "zero facilities" result, and
    wiping every cached row of a source on that basis would be worse than leaving stale ones.
    """
    if not ids:
        return 0
    result = con.execute("DELETE FROM campsites WHERE source = %s AND id <> ALL(%s)", [source, list(ids)])
    con.commit()
    if result.rowcount:
        _invalidate_rank_cache()  # see prune_campsites_outside_radius
    return result.rowcount
