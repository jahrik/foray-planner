"""Public-land polygon + trail table writes, and the trail<->public-land spatial
join that persists ``trails.land_agency``/``land_unit`` (issue #335 PR 2)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, LiteralString, cast

import psycopg

from foray.cache.core import _invalidate_rank_cache, upsert_rows

logger = logging.getLogger(__name__)

# The public-land point-in-polygon join trails.land_agency / land_unit persist (migration 48,
# issue #335 PR 2) - the smallest polygon covering a trail's representative point wins (a
# wilderness inside a forest beats the forest). Shared between the one-time migration backfill
# below and `_assign_trail_land`'s incremental recompute so the two queries can't drift apart.
# References `t2` - the caller's outer query must alias the trails row that way. Orders by
# `pl.area_deg2` (migration 49) - a column computed once per polygon write, not
# `ST_Area(pl.geom::geometry)` recomputed from scratch on every lookup (measured ~19ms/row at
# table scale, dominated by decompressing hundred-KB+ BLM/USFS polygons - see migration 49).
# Default page size for every trail<->land relabeling write (below) - bounds a single UPDATE to
# this many rows regardless of how many trails a land or trail upsert batch could otherwise touch.
_TRAIL_LAND_BATCH_SIZE = 5000
_TRAIL_LAND_JOIN: LiteralString = """
    LEFT JOIN LATERAL (
        SELECT pl.agency, pl.unit FROM public_land pl
        WHERE pl.geom IS NOT NULL
          AND ST_DWithin(pl.geom, ST_SetSRID(ST_MakePoint(t2.center_lng, t2.center_lat), 4326)::geography, 0)
        ORDER BY pl.area_deg2 LIMIT 1
    ) pl ON true
"""


def _assign_trail_land(
    con: psycopg.Connection,
    *,
    trail_ids: Sequence[str] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> int:
    """(Re)compute and persist ``trails.land_agency`` / ``land_unit`` (issue #335 PR 2) -
    this used to be a live point-in-polygon join run on every ``/api/trails`` request
    (``queries.trail_land_units`` / ``get_trail``, now retired in favor of reading these
    columns straight off ``trails``).

    ``trail_ids`` scopes to a just-upserted batch (called from :func:`upsert_trails` - a
    brand-new trail has no label yet). ``bbox`` scopes to the footprint of just-upserted land
    polygons (called from :func:`upsert_public_land` - an existing trail's label may have
    changed if a polygon covering it was added/changed); a trail whose representative point
    sits outside that box can't have been affected. Passing neither recomputes every trail -
    the one-time migration 48 backfill does this directly in SQL instead, since it runs before
    any Python code needing this function exists.
    """
    if trail_ids is not None and not trail_ids:
        return 0
    where: LiteralString
    params: list[Any]
    if trail_ids is not None:
        where = "WHERE t2.id = ANY(%s)"
        params = [list(trail_ids)]
    elif bbox is not None:
        min_lng, min_lat, max_lng, max_lat = bbox
        where = "WHERE t2.center_lng BETWEEN %s AND %s AND t2.center_lat BETWEEN %s AND %s"
        params = [min_lng, max_lng, min_lat, max_lat]
    else:
        where = ""
        params = []
    result = con.execute(
        f"""
        UPDATE trails t SET land_agency = land.agency, land_unit = land.unit
        FROM (
            SELECT t2.id, pl.agency, pl.unit
            FROM trails t2
            {_TRAIL_LAND_JOIN}
            {where}
        ) land
        WHERE land.id = t.id
        """,
        params,
    )
    return result.rowcount


def _assign_trail_land_paged(
    con: psycopg.Connection,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    batch_size: int = _TRAIL_LAND_BATCH_SIZE,
) -> int:
    """Page :func:`_assign_trail_land` over every trail in ``bbox`` (or the whole table when
    ``bbox`` is ``None``), ``batch_size`` ids at a time - each page its own committed UPDATE via
    ``_assign_trail_land``, never one UPDATE spanning however many trails the box covers.

    A Copilot review catch on PR #353: ``upsert_public_land`` used to hand ``_assign_trail_land``
    a raw bbox straight through, and a coverage-wide land refresh's bbox can span most of the
    country - that recreated the exact unbatched, un-interruptible write migration 48 was split
    up to avoid, just one call site later. This is also what :func:`backfill_trail_land` uses
    for its full-table pass (``bbox=None``).
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    where: LiteralString
    bbox_params: list[Any]
    if bbox is not None:
        min_lng, min_lat, max_lng, max_lat = bbox
        where = "AND center_lng BETWEEN %s AND %s AND center_lat BETWEEN %s AND %s"
        bbox_params = [min_lng, max_lng, min_lat, max_lat]
    else:
        where = ""
        bbox_params = []
    last_id = ""
    visited = 0
    while True:
        batch_ids = [
            row[0]
            for row in con.execute(
                f"SELECT id FROM trails WHERE id > %s {where} ORDER BY id LIMIT %s",
                [last_id, *bbox_params, batch_size],
            ).fetchall()
        ]
        if not batch_ids:
            return visited
        _assign_trail_land(con, trail_ids=batch_ids)
        visited += len(batch_ids)
        last_id = batch_ids[-1]


def backfill_trail_land(con: psycopg.Connection) -> int:
    """One-time backfill of ``trails.land_agency`` / ``land_unit`` for every trail cached before
    migration 48 shipped (issue #335 PR 2's ``foray backfill-trail-land`` CLI command).

    Migration 48 only adds the columns - deliberately no backfill UPDATE there, see its comment
    in ``_MIGRATIONS``. ``_assign_trail_land`` keeps *new* writes current from then on, but a
    trail already cached (and whose ingest marker already says "done", so it won't naturally
    re-upsert) needs this run once.

    Polygon-driven, not trail-driven - a second perf fix on top of migration 49's ``area_deg2``.
    The point-driven ``_assign_trail_land`` decompresses every *candidate* polygon's full
    geometry for every trail it's tested against - for a handful of huge polygons (Tongass
    National Forest alone is ~430 KB of GeoJSON) shared by thousands of trails, that repeats the
    same expensive decompression once per trail underneath it (measured ~19ms/trail at table
    scale even after migration 49). This instead walks ``public_land`` once, smallest-area-first
    (so a wilderness inside a forest still wins), and for each polygon labels every *unlabeled*
    trail inside it in one UPDATE - each polygon's geometry is decompressed once total, not once
    per trail. ``WHERE t.land_agency IS NULL`` is what makes "smallest first, never overwrite"
    equivalent to "smallest polygon wins": a trail already labeled by an earlier (smaller)
    polygon in this same pass is left alone.

    This is correct specifically *because* every trail starts NULL going into a backfill - the
    ongoing incremental hooks (``upsert_trails``, ``upsert_public_land``) still use the
    point-driven ``_assign_trail_land``, which unconditionally overwrites and so stays correct
    when a polygon shrinks or moves after already labeling a trail (see ``upsert_public_land``'s
    pre/post bbox union). Needs ``ix_trails_center_point_geog`` (a CONCURRENTLY-built expression
    index, ``_CONCURRENT_INDEXES``) to find each polygon's candidate trails without a table scan;
    degrades to a slower plan without it - ``apply_schema`` treats every ``_CONCURRENT_INDEXES``
    entry as a query-speed optimization, never a correctness dependency.

    Each polygon's UPDATE is its own autocommitted statement (``con`` is autocommit) - safe to
    interrupt (whatever already committed stays done) and safe to re-run (an already-labeled
    trail is skipped, not re-touched). Returns rows labeled.
    """
    land_ids = [
        row[0]
        for row in con.execute(
            "SELECT id FROM public_land WHERE geom IS NOT NULL ORDER BY area_deg2 ASC NULLS LAST"
        ).fetchall()
    ]
    visited = 0
    for land_id in land_ids:
        result = con.execute(
            """
            UPDATE trails t SET land_agency = pl.agency, land_unit = pl.unit
            FROM public_land pl
            WHERE pl.id = %s
              AND t.land_agency IS NULL
              AND ST_DWithin(pl.geom, ST_SetSRID(ST_MakePoint(t.center_lng, t.center_lat), 4326)::geography, 0)
            """,
            [land_id],
        )
        visited += result.rowcount
    logger.info("land: backfilled trail<->land for %d trails across %d public_land polygons", visited, len(land_ids))
    return visited


def _public_land_bbox(con: psycopg.Connection, ids: Sequence[str]) -> tuple[float, float, float, float] | None:
    """Bounding box of the cached ``public_land`` rows named by ``ids``, or ``None`` if none of
    them currently have a row (either they're new, or - called before the upsert - never existed)."""
    row = con.execute(
        "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) "
        "FROM (SELECT ST_Extent(geom::geometry) AS e FROM public_land WHERE id = ANY(%s)) box",
        [list(ids)],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return cast(tuple[float, float, float, float], row)


def _union_bbox(
    a: tuple[float, float, float, float] | None, b: tuple[float, float, float, float] | None
) -> tuple[float, float, float, float] | None:
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def upsert_public_land(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert public-land polygons, refreshing existing rows in place. Returns rows attempted.

    Each tuple is (id, agency, unit, source, url, geojson). Relabels any trail that may fall
    inside the changed footprint (see :func:`_assign_trail_land_paged`) - scoped to the union of
    the *pre*- and *post*-upsert bbox of just the rows upserted here, not every cached polygon,
    so a home-radius land refresh doesn't force a full-table trail scan. The pre-upsert bbox
    matters too (a Copilot review catch, PR #353): a polygon that shrank or moved would otherwise
    leave a trail in its *old* footprint mislabeled forever, since the new bbox alone never covers
    where that trail actually sits.
    """
    columns: tuple[LiteralString, ...] = ("id", "agency", "unit", "source", "url", "geojson")
    ids = [row[0] for row in rows]
    old_bbox = _public_land_bbox(con, ids) if ids else None
    result = upsert_rows(con, "public_land", columns, rows)
    if ids:
        new_bbox = _public_land_bbox(con, ids)
        bbox = _union_bbox(old_bbox, new_bbox)
        if bbox is not None:
            _assign_trail_land_paged(con, bbox=bbox)
    _invalidate_rank_cache()
    return result


def upsert_trails(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert trail tuples, refreshing existing rows in place. Returns rows attempted.

    Each tuple is (id, name, kind, source, url, center_lat, center_lng, geojson, connects,
    length_km, attrs) - ``connects`` is set on trailhead rows, ``length_km``/``attrs`` on
    path/route rows, ``None`` on the other kind. External shape is unchanged (still one
    ``geojson`` element per tuple) despite the write fanning out to two tables internally
    (issue #333 PR 2 - the ``trail_geometry`` split, migration 45): callers keep passing a
    single 11-tuple, this just routes ``geojson`` to ``trail_geometry`` instead of `trails`.

    ``trails`` is upserted *before* ``trail_geometry`` - the latter's ``AFTER`` trigger
    (``foray_trail_geom_from_geometry``) derives ``trails.geom`` from the geojson via an
    ``UPDATE trails ... WHERE id = NEW.id``, which needs the ``trails`` row to already exist.
    """
    trails_columns: tuple[LiteralString, ...] = (
        "id",
        "name",
        "kind",
        "source",
        "url",
        "center_lat",
        "center_lng",
        "connects",
        "length_km",
        "attrs",
    )
    trails_rows = [(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[8], row[9], row[10]) for row in rows]
    geometry_rows = [(row[0], row[7]) for row in rows]
    result = upsert_rows(con, "trails", trails_columns, trails_rows)
    upsert_rows(con, "trail_geometry", ("id", "geojson"), geometry_rows)
    ids = [row[0] for row in rows]
    for i in range(0, len(ids), _TRAIL_LAND_BATCH_SIZE):
        _assign_trail_land(con, trail_ids=ids[i : i + _TRAIL_LAND_BATCH_SIZE])
    _invalidate_rank_cache()
    return result


def prune_duplicate_route_paths(
    con: psycopg.Connection, *, min_lat: float, min_lng: float, max_lat: float, max_lng: float
) -> int:
    """Delete OSM ``path`` rows in this bbox that duplicate an OSM ``route`` row's own
    member-way geometry (issue #394) - the self-healing complement to
    ``trails._parse_trails``'s ingest-time dedup, which only stops *new* duplicate rows from
    being cached, not clean up ones already cached under an older ``_TRAILS_QUERY_VERSION``.
    Every trails ingest (``ingest_trails``, ``ingest_trails_region``) calls this right after its
    own upsert, scoped to that call's own area, so the weekly ``refresh --with trails --all``
    cron (already re-pulling every region on a version bump) converges the whole cache on its
    normal schedule - no one-off migration or manual ops step.

    ``source = 'osm'`` on both sides (Copilot review, PR #396) - ``trails`` also holds
    authoritative bulk-loaded rows (``usfs_trails.py``'s Trail_NFS import, ``source='usfs'``,
    also cached as ``kind='path'``); those two sources intentionally overlap in places and
    neither should ever prune the other's rows just because an OSM route happens to cover the
    same physical trail.

    Scoped, not table-wide: a nationwide sweep has to ``ST_Buffer`` every cached route -
    including genuinely huge ones (a 675km hiking route) - against every candidate path row.
    That's what this issue's first fix shipped as a global migration, and it took the prod
    droplet's SSH connection down mid-deploy (buffering ~8.3k routes, some enormous, plus the
    resulting nested-loop join, ran past the ansible task's patience). Scoping to one ingest
    call's bbox keeps each call's route set to a handful - cheap enough to run on every ingest,
    and (measured) cheap enough at that scale to buffer on ``geography`` directly rather than
    the ``geometry`` cast with a hand-rolled degree tolerance the first version of this function
    used - that planar approximation used one degree size for both axes (`KM_PER_DEG_LAT` for
    longitude too), understating the true east-west tolerance away from the equator (Copilot
    review, PR #396: ~3.4m instead of 5m at this app's ~47N latitude) and so missing some real
    duplicates. ``geography``'s buffer is isotropic - a true 5m radius everywhere - with no
    latitude correction to get wrong.
    """
    envelope = "ST_MakeEnvelope(%s, %s, %s, %s, 4326)::geography"
    envelope_params = [min_lng, min_lat, max_lng, max_lat]
    result = con.execute(
        f"""
        WITH area_routes AS MATERIALIZED (
            SELECT geom AS route_geom, ST_Buffer(geom, 5) AS buf
            FROM trails
            WHERE kind = 'route' AND source = 'osm' AND geom && {envelope}
        )
        DELETE FROM trails p
        USING area_routes r
        WHERE p.kind = 'path'
          AND p.source = 'osm'
          AND p.geom && {envelope}
          AND ST_DWithin(p.geom, r.route_geom, 5)
          AND ST_CoveredBy(p.geom, r.buf)
        """,
        [*envelope_params, *envelope_params],
    )
    con.commit()
    if result.rowcount:
        _invalidate_rank_cache()
    return result.rowcount


def prune_trails_missing_from(con: psycopg.Connection, source: str, ids: Sequence[str]) -> int:
    """Delete ``source`` trails whose id isn't in ``ids``. Returns rows deleted.

    For a source loaded from an authoritative full dump (the USFS Trail_NFS bulk snapshot,
    issue #335 PR 3a) every row it lists is the complete truth, same as
    ``prune_campsites_missing_from`` for the RIDB bulk snapshot - a trail the export no longer
    lists (decommissioned, rerouted onto a new id) has to go. Deletes nothing if ``ids`` is
    empty - an empty load is far more likely a bug than a real "zero trails" result, and wiping
    every cached row of a source on that basis would be worse than leaving stale ones.
    ``trail_geometry`` cascades via its FK (``ON DELETE CASCADE``), so no separate delete there.
    """
    if not ids:
        return 0
    result = con.execute("DELETE FROM trails WHERE source = %s AND id <> ALL(%s)", [source, list(ids)])
    con.commit()
    if result.rowcount:
        _invalidate_rank_cache()
    return result.rowcount
