"""Public-land polygon + trail table writes, and the trail<->public-land spatial
join that persists ``trails.land_agency``/``land_unit`` (issue #335 PR 2)."""

from __future__ import annotations

import hashlib
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
# Point-in-polygon on `geometry`, with the `geography` GIST index (`&&`) as the prefilter. The
# old `ST_DWithin(geography, geography, 0)` spent ~76ms per trail on the huge, many-vertex
# national-forest polygons (measured locally) - 3,000 freshly ingested forest roads held one
# UPDATE for ~4 min, which made a full trails re-pull take days; the planar test is ~3.5x
# faster with identical results on 800 sampled trails (a containment test on a point doesn't
# need the spheroid).
_TRAIL_LAND_JOIN: LiteralString = """
    LEFT JOIN LATERAL (
        SELECT pl.agency, pl.unit FROM public_land pl
        WHERE pl.geom && ST_SetSRID(ST_MakePoint(t2.center_lng, t2.center_lat), 4326)::geography
          AND ST_Intersects(pl.geom::geometry, ST_SetSRID(ST_MakePoint(t2.center_lng, t2.center_lat), 4326))
        ORDER BY pl.area_deg2 LIMIT 1
    ) pl ON true
"""
# The same lookup against `public_land_parts` (migration 54, issue #442): each polygon cut into
# pieces of at most `_LAND_PART_VERTICES` vertices, so the containment test is cheap. ~0.3 ms per
# trail vs ~20 ms on whole polygons, identical results on 1,000 sampled trails. `area_deg2` is the
# whole parcel's, so "smallest owning unit" still means the same thing.
_TRAIL_LAND_PARTS_JOIN: LiteralString = """
    LEFT JOIN LATERAL (
        SELECT lp.agency, lp.unit FROM public_land_parts lp
        WHERE ST_Intersects(lp.geom, ST_SetSRID(ST_MakePoint(t2.center_lng, t2.center_lat), 4326))
        ORDER BY lp.area_deg2 LIMIT 1
    ) pl ON true
"""
_LAND_PART_VERTICES = 128
_LAND_PARTS_META_KEY = "public_land_parts"


def _land_parts_ready(con: psycopg.Connection) -> bool:
    row = con.execute("SELECT value FROM meta WHERE key = %s", [_LAND_PARTS_META_KEY]).fetchone()
    return row is not None and row[0] == "ready"


def ensure_land_parts(con: psycopg.Connection) -> bool:
    """Build ``public_land_parts`` from every ``public_land`` polygon if it hasn't been built
    yet. Returns True if it built. One transaction, so the trail land lookup never sees a
    half-built table - it reads ``meta`` and keeps using whole polygons until this commits.
    Called by the jobs that tag trails in bulk (the trails bulk loads, ``backfill-trail-land``)
    and by the land refresh; never from a deploy migration (geometry work)."""
    if _land_parts_ready(con):
        return False
    with con.transaction():
        con.execute("TRUNCATE public_land_parts")
        con.execute(
            """
            INSERT INTO public_land_parts (land_id, agency, unit, area_deg2, geom)
            SELECT id, agency, unit, area_deg2, ST_Subdivide(geom::geometry, %s)
            FROM public_land WHERE geom IS NOT NULL
            """,
            [_LAND_PART_VERTICES],
        )
        con.execute(
            "INSERT INTO meta (key, value) VALUES (%s, 'ready') ON CONFLICT (key) DO UPDATE SET value = 'ready'",
            [_LAND_PARTS_META_KEY],
        )
    logger.info("public_land_parts: built from every public_land polygon")
    return True


def _rebuild_land_parts(con: psycopg.Connection, land_ids: Sequence[str]) -> None:
    """Replace the pieces of just-upserted ``public_land`` rows (deleted rows cascade)."""
    con.execute("DELETE FROM public_land_parts WHERE land_id = ANY(%s)", [list(land_ids)])
    con.execute(
        """
        INSERT INTO public_land_parts (land_id, agency, unit, area_deg2, geom)
        SELECT id, agency, unit, area_deg2, ST_Subdivide(geom::geometry, %s)
        FROM public_land WHERE id = ANY(%s) AND geom IS NOT NULL
        """,
        [_LAND_PART_VERTICES, list(land_ids)],
    )


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
    land_join = _TRAIL_LAND_PARTS_JOIN if _land_parts_ready(con) else _TRAIL_LAND_JOIN
    # LLVM JIT compiles this per call and costs more than the lookup itself (measured: 3,000
    # trails took 7.2 s with JIT on, 1.0 s off) - same fix as `scoring.queries`' land pins.
    with con.transaction():
        con.execute("SET LOCAL jit = off")
        result = con.execute(
            f"""
            UPDATE trails t SET land_agency = land.agency, land_unit = land.unit
            FROM (
                SELECT t2.id, pl.agency, pl.unit
                FROM trails t2
                {land_join}
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
    if ids and not ensure_land_parts(con) and _land_parts_ready(con):
        _rebuild_land_parts(con, ids)
    if ids:
        new_bbox = _public_land_bbox(con, ids)
        bbox = _union_bbox(old_bbox, new_bbox)
        if bbox is not None:
            _assign_trail_land_paged(con, bbox=bbox)
    _invalidate_rank_cache()
    return result


# A trail row's identity for a ``trail_duplicates`` tombstone: what the dedup judged it on (name,
# attrs - a forest road's ``ref`` - and geometry). The SQL and Python forms must hash the same
# bytes; a pruned row's fingerprint is taken in SQL as it's deleted, an incoming row's here.
_FINGERPRINT_SQL: LiteralString = (
    "md5(concat_ws('|', coalesce(p.name, ''), coalesce(p.attrs, ''), "
    "coalesce((SELECT g.geojson FROM trail_geometry g WHERE g.id = p.id), '')))"
)


def _row_fingerprint(row: tuple[Any, ...]) -> str:
    return hashlib.md5(
        "|".join((row[1] or "", row[10] or "", row[7] or "")).encode(), usedforsecurity=False
    ).hexdigest()


def tombstoned_ids(con: psycopg.Connection, ids: Sequence[str]) -> set[str]:
    """The ``ids`` a ``trail_duplicates`` tombstone currently keeps out of ``trails``."""
    if not ids:
        return set()
    found = con.execute("SELECT osm_id FROM trail_duplicates WHERE osm_id = ANY(%s)", [list(ids)]).fetchall()
    return {row[0] for row in found}


def _drop_known_duplicates(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """``rows`` minus those a dedup prune already replaced, with each remaining trailhead's
    ``connects`` pointed at the surviving rows (deduped, sorted).

    A tombstone only holds while the incoming row is the one that was pruned (Copilot review,
    PR #443): an OSM edit that renames or reroutes the way no longer matches the fingerprint, so
    the tombstone is released and the row written - the caller's dedup pass judges it afresh."""
    lookup = {row[0] for row in rows}
    for row in rows:
        lookup.update(row[8] or ())
    if not lookup:
        return list(rows)
    tombstones = {
        osm_id: (kept_id, fingerprint)
        for osm_id, kept_id, fingerprint in con.execute(
            "SELECT osm_id, kept_id, fingerprint FROM trail_duplicates WHERE osm_id = ANY(%s)", [list(lookup)]
        ).fetchall()
    }
    if not tombstones:
        return list(rows)
    stale = [row[0] for row in rows if row[0] in tombstones and tombstones[row[0]][1] != _row_fingerprint(row)]
    if stale:
        con.execute("DELETE FROM trail_duplicates WHERE osm_id = ANY(%s)", [stale])
        for osm_id in stale:
            del tombstones[osm_id]
    replaced = {osm_id: kept_id for osm_id, (kept_id, _fingerprint) in tombstones.items()}
    kept: list[tuple[Any, ...]] = []
    for row in rows:
        if row[0] in replaced:
            continue
        if row[8]:
            row = (*row[:8], sorted({replaced.get(linked, linked) for linked in row[8]}), *row[9:])
        kept.append(row)
    return kept


def upsert_trails_changed(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> list[str]:
    """:func:`upsert_trails` only the rows that are new or differ from what's cached. Returns
    the ids written.

    The bulk loaders' diff path (issue #442): a weekly national snapshot is overwhelmingly
    unchanged rows, and every write costs a land-ownership lookup and a geometry trigger on the
    1-vCPU database - rewriting all of it every week is what made the MVUM load take ~2.4 h.
    Compared column by column (and ``trail_geometry.geojson``), which works because every
    source builds its row tuple deterministically from the same inputs. Tombstoned duplicates
    are dropped first, so they never count as "new".

    A rewritten row releases the tombstones of the twins it replaced (Copilot review, PR #443):
    once a USFS trail or OSM route is rerouted, the old verdict no longer stands, so the twin is
    let back in on its next load and the dedup decides again."""
    rows = _drop_known_duplicates(con, rows)
    if not rows:
        return []
    con.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _incoming_trails (
            id TEXT PRIMARY KEY, name TEXT, kind TEXT, source TEXT, url TEXT,
            center_lat DOUBLE PRECISION, center_lng DOUBLE PRECISION, geojson TEXT,
            connects TEXT[], length_km DOUBLE PRECISION, attrs TEXT
        )
        """
    )
    con.execute("TRUNCATE _incoming_trails")
    with con.cursor().copy(
        "COPY _incoming_trails (id, name, kind, source, url, center_lat, center_lng, geojson, connects, "
        "length_km, attrs) FROM STDIN"
    ) as copy:
        for row in {row[0]: row for row in rows}.values():
            copy.write_row(row)
    changed_ids = {
        row[0]
        for row in con.execute(
            """
            SELECT i.id
            FROM _incoming_trails i
            LEFT JOIN trails t ON t.id = i.id
            LEFT JOIN trail_geometry g ON g.id = i.id
            WHERE t.id IS NULL
               OR (t.name, t.kind, t.source, t.url, t.center_lat, t.center_lng, t.connects, t.length_km, t.attrs)
                  IS DISTINCT FROM
                  (i.name, i.kind, i.source, i.url, i.center_lat, i.center_lng, i.connects, i.length_km, i.attrs)
               OR g.geojson IS DISTINCT FROM i.geojson
            """
        ).fetchall()
    }
    con.execute("TRUNCATE _incoming_trails")
    changed = [row for row in rows if row[0] in changed_ids]
    if changed:
        upsert_trails(con, changed)
        con.execute("DELETE FROM trail_duplicates WHERE kept_id = ANY(%s)", [list(changed_ids)])
    return [row[0] for row in changed]


def upsert_trails(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert trail tuples, refreshing existing rows in place. Returns rows attempted.

    Each tuple is (id, name, kind, source, url, center_lat, center_lng, geojson, connects,
    length_km, attrs) - ``connects`` is set on trailhead rows, ``length_km``/``attrs`` on
    path/route rows, ``None`` on the other kind. External shape is unchanged (still one
    ``geojson`` element per tuple) despite the write fanning out to two tables internally
    (issue #333 PR 2 - the ``trail_geometry`` split, migration 45): callers keep passing a
    single 11-tuple, this just routes ``geojson`` to ``trail_geometry`` instead of `trails`.

    Rows a dedup prune already resolved (a ``trail_duplicates`` tombstone, issue #442) are
    dropped, and trailhead ``connects`` are remapped onto the surviving rows - see
    :func:`_drop_known_duplicates`. Every writer (OSM bulk load, home-radius ingest, live
    trailhead resolution, USFS loads) comes through here, so none of them can re-add a twin.

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
    rows = _drop_known_duplicates(con, rows)
    if not rows:
        return 0
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
            SELECT id AS route_id, geom AS route_geom, ST_Buffer(geom, 5) AS buf
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
        RETURNING p.id, r.route_id, {_FINGERPRINT_SQL}
        """,
        [*envelope_params, *envelope_params],
    )
    replaced = result.fetchall()
    _record_replacements(con, replaced)
    con.commit()
    if replaced:
        _invalidate_rank_cache()
    return len(replaced)


def _record_replacements(con: psycopg.Connection, replaced: Sequence[tuple[str, str, str]]) -> None:
    """Bookkeeping for rows a dedup prune just deleted, as (deleted id, surviving id, deleted
    row's fingerprint) triples.

    - A ``trail_duplicates`` tombstone per pair (issue #442), so the next write of the same
      source row - every weekly OSM snapshot still lists it - is dropped by ``upsert_trails``
      instead of re-inserted and pruned all over again - as long as it's still the row that was
      pruned (the fingerprint, see :func:`_drop_known_duplicates`).
    - Trailhead ``connects`` arrays pointing at a deleted id move to its replacement (Copilot
      review, PR #441): ``resolve_trail_network`` / ``connected_trails`` drop ids that no longer
      exist, so without this a pruned twin silently truncated a trailhead's network - or, with
      every linked row gone, forced a live Overpass lookup on the next selection."""
    if not replaced:
        return
    old_ids = [old_id for old_id, _new_id, _fingerprint in replaced]
    new_ids = [new_id for _old_id, new_id, _fingerprint in replaced]
    fingerprints = [fingerprint for _old_id, _new_id, fingerprint in replaced]
    con.execute(
        """
        INSERT INTO trail_duplicates (osm_id, kept_id, fingerprint)
        SELECT * FROM unnest(%s::text[], %s::text[], %s::text[])
        ON CONFLICT (osm_id) DO UPDATE SET kept_id = EXCLUDED.kept_id, fingerprint = EXCLUDED.fingerprint
        """,
        [old_ids, new_ids, fingerprints],
    )
    _remap_connects(con, old_ids, new_ids)


def _remap_connects(con: psycopg.Connection, old_ids: list[str], new_ids: list[str]) -> None:
    """Rewrite every trailhead ``connects`` entry in ``old_ids`` to its ``new_ids`` counterpart."""
    con.execute(
        """
        UPDATE trails t
        SET connects = ARRAY(
            SELECT DISTINCT coalesce(swap.new_id, linked.id)
            FROM unnest(t.connects) AS linked(id)
            LEFT JOIN unnest(%s::text[], %s::text[]) AS swap(old_id, new_id) ON swap.old_id = linked.id
            ORDER BY 1
        )
        WHERE t.connects && %s::text[]
        """,
        [old_ids, new_ids, old_ids],
    )


# Words too generic to say two trail names refer to the same trail ("Ridge Trail" vs "Creek
# Trail"); prune_duplicate_cross_source_paths' name guard ignores them.
_GENERIC_TRAIL_WORDS = ("trail", "trails", "the", "and", "loop", "tie", "path", "nrt", "connector", "spur")


def prune_duplicate_cross_source_paths(
    con: psycopg.Connection, *, min_lat: float, min_lng: float, max_lat: float, max_lng: float
) -> int:
    """Delete an OSM ``path`` row in this bbox that duplicates a USFS ``path`` row's geometry
    (issue #404) - the cross-source sibling of ``prune_duplicate_route_paths``. That function
    deliberately never touches a ``source='usfs'`` row (Copilot review, PR #396); this is the
    fix for the case it left open - the same physical trail cached separately from OSM and from
    the USFS Trail_NFS bulk import (``usfs_trails.py``), confirmed live for Mule Mountain Trail
    #919 (``osm:way/5159158`` vs ``usfs:trail/5031.005121``).

    USFS wins on a match: it's the authoritative source, matching the precedent already set for
    the road layer (MVUM preferred over OSM's guessed vehicle-legality tags). Only the OSM row
    is ever deleted here - a USFS row is never pruned for duplicating an OSM one.

    Two independently-digitized lines for the same physical trail don't align anywhere near as
    tightly as an OSM route and its own member way (``prune_duplicate_route_paths``'s 5m). The
    Mule Mountain pair's *directed* distance - every OSM vertex's distance to the nearest point
    on the USFS line, which is what ``ST_CoveredBy`` against a buffered USFS line actually
    tests - measures ~123m (queried live; the symmetric Hausdorff distance, ~140m, is a looser,
    wrong number for this check since it's dominated by the reverse direction). A 50m buffer
    (Copilot review, PR #405 first draft) can never match this real case; 150m clears it with
    margin. That's still a targeted tolerance, not an unbounded one - it's paired with two
    checks a same-source match doesn't need, both required to guard against false merges
    between genuinely distinct trails: the OSM path's *entire* length must fall within the
    buffer (``ST_CoveredBy``, not just endpoints), and the stretch of USFS line running
    alongside it - the part inside a flat-ended 150m buffer of the OSM way - must be within 30%
    of the OSM way's own length. A spur branching off the USFS trail fails that second check
    (the USFS line crosses its buffer sideways, not along it, so the alongside stretch is the
    buffer's width, not the spur's length). And when both rows carry a real name (not the
    synthetic ``... (OSM)`` / ``USFS trail`` fallbacks) they must share a non-generic word -
    dense MTB networks (Bend's Phil's complex) run distinct named trails ~100m apart, inside
    that tolerance.

    The comparison is against that alongside stretch, not the whole USFS row: OSM splits one
    trail into many ways (at every junction / tag change) while a Trail_NFS row is usually the
    whole trail, so a first version comparing ``length_km`` to the whole USFS row never matched
    an OSM way that is only one piece of it - the common case, and most of the duplicates still
    drawn on the map after #404 shipped.

    Scoped per ingest-call bbox, same reasoning as ``prune_duplicate_route_paths`` - a
    table-wide sweep already took prod down once for the same-source case; this only ever runs
    against the handful of USFS rows in one tile.
    """
    name_guard: LiteralString = """
        p.name IS NULL OR p.name LIKE '%%(OSM)' OR u.name IS NULL OR u.name = 'USFS trail'
        OR EXISTS (
            SELECT 1 FROM regexp_split_to_table(lower(p.name), '[^a-z]+') AS word
            WHERE length(word) > 2
              AND word <> ALL (%s)
              AND word = ANY (regexp_split_to_array(lower(u.name), '[^a-z]+'))
        )
    """
    return _prune_cross_source(
        con,
        bbox=(min_lat, min_lng, max_lat, max_lng),
        kind="path",
        usfs_source="usfs",
        guard_sql=name_guard,
        guard_params=[list(_GENERIC_TRAIL_WORDS)],
    )


# Strips the agency prefix OSM puts on a forest-road number ("NF-2710", "FR 27N07A") so it
# compares equal to MVUM's bare route number ("2710", "27N07A").
def _road_ref_sql(column: LiteralString) -> LiteralString:
    return "regexp_replace(upper(" + column + "), '^(USFS|NFSR|NF|FR|FS)?[^A-Z0-9]*|[^A-Z0-9]', '', 'g')"


def prune_duplicate_cross_source_roads(
    con: psycopg.Connection, *, min_lat: float, min_lng: float, max_lat: float, max_lng: float
) -> int:
    """Delete an OSM forest ``road`` row in this bbox that duplicates a USFS MVUM road - the road
    sibling of ``prune_duplicate_cross_source_paths``, same geometry test (150m alongside rule)
    and same direction (MVUM wins: it's the authoritative Forest Service record, and the one
    carrying the vehicle-legality matrix).

    Measured on prod (northern CA, where both are cached): an OSM forest road sits a median
    ~18m and a 90th-percentile ~95m from its MVUM twin, so the trail tolerance fits. Names never
    line up (OSM uses the route number, MVUM a descriptive name - "27N80" vs "HUMBOLDT NORTH
    WEST"), so the guard compares route numbers instead: when both rows carry a ``ref`` they
    must match once OSM's agency prefix is stripped. Most OSM forest roads carry no ref and are
    matched on geometry alone.
    """
    ref_guard: LiteralString = (
        "coalesce(p.attrs::jsonb->>'ref', '') = '' OR coalesce(u.attrs::jsonb->>'ref', '') = '' OR "
        + _road_ref_sql("p.attrs::jsonb->>'ref'")
        + " = "
        + _road_ref_sql("u.attrs::jsonb->>'ref'")
    )
    return _prune_cross_source(
        con,
        bbox=(min_lat, min_lng, max_lat, max_lng),
        kind="road",
        usfs_source="usfs_mvum",
        guard_sql=ref_guard,
        guard_params=[],
    )


# ~1 km - comfortably past the 150m match tolerance at any latitude in coverage.
_CROSS_SOURCE_PAD_DEG = 0.01


def _prune_cross_source(
    con: psycopg.Connection,
    *,
    bbox: tuple[float, float, float, float],
    kind: str,
    usfs_source: str,
    guard_sql: LiteralString,
    guard_params: list[Any],
) -> int:
    """Shared body of the two cross-source prunes: delete each OSM ``kind`` row in ``bbox`` fully
    inside a 150m buffer of a ``usfs_source`` row, whose alongside stretch of that USFS line is
    within 30% of its own length, that runs *along* it rather than away from it, and that passes
    ``guard_sql`` (``p`` = OSM row, ``u`` = USFS). Trailhead links to a deleted row move to the
    USFS row that replaced it.

    "Along": the OSM way's two ends sit at nearly the same distance from the USFS line (they
    may differ by at most a quarter of its length). A spur leaving the trail obliquely starts on
    it and ends away from it - Copilot review, PR #441: an unnamed 100m spur at 30 degrees fits
    inside the buffer and its alongside stretch (~115m) passes the length check, but its ends
    are ~0m and ~50m off the line. A real twin keeps a roughly steady offset. A
    MultiLineString's start/end point is NULL, which fails the check - kept, never guessed."""
    min_lat, min_lng, max_lat, max_lng = bbox
    envelope = "ST_MakeEnvelope(%s, %s, %s, %s, 4326)::geography"
    envelope_params = [min_lng, min_lat, max_lng, max_lat]
    # USFS candidates come from a slightly wider box than the OSM rows judged: a twin can lie just
    # across the edge (an adjacent ingest tile, or beside a due-east line with a zero-height bbox).
    pad = _CROSS_SOURCE_PAD_DEG
    usfs_envelope_params = [min_lng - pad, min_lat - pad, max_lng + pad, max_lat + pad]
    result = con.execute(
        f"""
        WITH u AS MATERIALIZED (
            SELECT id AS usfs_id, name, attrs, geom, ST_Buffer(geom, 150) AS buf
            FROM trails
            WHERE kind = %s AND source = %s AND geom && {envelope}
        ),
        covered AS (
            SELECT
                p.id,
                u.usfs_id,
                ST_Length(p.geom) AS osm_m,
                ST_Length(ST_Intersection(u.geom, ST_Buffer(p.geom, 150, 'endcap=flat'))) AS alongside_m,
                ST_Distance(ST_StartPoint(p.geom::geometry)::geography, u.geom) AS start_off_m,
                ST_Distance(ST_EndPoint(p.geom::geometry)::geography, u.geom) AS end_off_m
            FROM trails p
            JOIN u ON ST_CoveredBy(p.geom, u.buf)
            WHERE p.kind = %s AND p.source = 'osm' AND p.geom && {envelope}
              AND ({guard_sql})
        ),
        matched AS (
            SELECT DISTINCT ON (id) id, usfs_id
            FROM covered
            WHERE osm_m > 0
              AND abs(alongside_m - osm_m) <= 0.3 * greatest(alongside_m, osm_m)
              AND abs(end_off_m - start_off_m) <= 0.25 * osm_m
            ORDER BY id, abs(alongside_m - osm_m)
        )
        DELETE FROM trails p
        USING matched m
        WHERE p.id = m.id
        RETURNING p.id, m.usfs_id, {_FINGERPRINT_SQL}
        """,
        [kind, usfs_source, *usfs_envelope_params, kind, *envelope_params, *guard_params],
    )
    replaced = result.fetchall()
    _record_replacements(con, replaced)
    con.commit()
    if replaced:
        _invalidate_rank_cache()
    return len(replaced)


def prune_trail_duplicates(
    con: psycopg.Connection, *, min_lat: float, min_lng: float, max_lat: float, max_lng: float
) -> int:
    """The one dedup pass for ``trails`` in a bbox: OSM path rows that repeat an OSM route's own
    member ways (#394), and OSM paths / forest roads that repeat a USFS Trail_NFS / MVUM record
    (#404, #440). The table is the single place duplicates are resolved - the map's vector tiles
    and every ``/api/trails`` read draw straight from it (there used to be a separate 15m
    read-time filter in ``scoring.queries`` too, with its own tolerance, so a list and the map
    could disagree). Called after every write that can introduce a duplicate: each OSM trails
    ingest tile, and each USFS bulk load (:func:`prune_trail_duplicates_tiled`)."""
    bounds = {"min_lat": min_lat, "min_lng": min_lng, "max_lat": max_lat, "max_lng": max_lng}
    return (
        prune_duplicate_route_paths(con, **bounds)
        + prune_duplicate_cross_source_paths(con, **bounds)
        + prune_duplicate_cross_source_roads(con, **bounds)
    )


_PRUNE_TILE_DEG = 2.0
_PRUNE_ID_BATCH = 50_000


def prune_trail_duplicates_tiled(con: psycopg.Connection, source: str, ids: Sequence[str] | None = None) -> int:
    """Run :func:`prune_trail_duplicates` over every ``_PRUNE_TILE_DEG`` tile a ``source`` row
    crosses - or, given ``ids``, only the tiles those rows cross (a bulk loader's new/changed
    rows, issue #442: a routine weekly load shouldn't re-check the whole country). What a bulk
    loader calls after loading, since a newly loaded row can duplicate one cached long before.
    Tile by tile, each its own committed statement, never one table-wide sweep - see
    :func:`prune_duplicate_route_paths` for the prod outage a global geometry sweep caused."""
    if ids is not None and not ids:
        return 0
    id_filter: LiteralString = "AND id = ANY(%s)" if ids is not None else ""
    # Ids go in batches: a national first load writes millions, and one array that size made
    # Postgres hash it in shared memory and fail on a 64 MB /dev/shm (dev's container).
    id_batches: list[list[str]] | list[None] = (
        [list(ids[start : start + _PRUNE_ID_BATCH]) for start in range(0, len(ids), _PRUNE_ID_BATCH)]
        if ids is not None
        else [None]
    )
    cells: set[tuple[int, int]] = set()
    for id_batch in id_batches:
        params: list[Any] = [_PRUNE_TILE_DEG, _PRUNE_TILE_DEG, _PRUNE_TILE_DEG, _PRUNE_TILE_DEG, source]
        if id_batch is not None:
            params.append(id_batch)
        # Every tile each line's bbox crosses, not just the tile holding its centre - an OSM twin
        # of a long line's far end lies wholly outside the centre's tile (Copilot review, PR #441).
        cells.update(
            con.execute(
                f"""
                SELECT DISTINCT lat_cell, lng_cell
                FROM trails,
                     generate_series(
                         floor(ST_YMin(geom::geometry) / %s)::int, floor(ST_YMax(geom::geometry) / %s)::int
                     ) AS lat_cell,
                     generate_series(
                         floor(ST_XMin(geom::geometry) / %s)::int, floor(ST_XMax(geom::geometry) / %s)::int
                     ) AS lng_cell
                WHERE source = %s AND geom IS NOT NULL {id_filter}
                """,
                params,
            ).fetchall()
        )
    total = 0
    for lat_cell, lng_cell in sorted(cells):
        south, west = lat_cell * _PRUNE_TILE_DEG, lng_cell * _PRUNE_TILE_DEG
        total += prune_trail_duplicates(
            con, min_lat=south, min_lng=west, max_lat=south + _PRUNE_TILE_DEG, max_lng=west + _PRUNE_TILE_DEG
        )
    logger.info("trails: pruned %d duplicate rows across %d tiles holding %s rows", total, len(cells), source)
    return total


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
    id_list = list(ids)
    result = con.execute("DELETE FROM trails WHERE source = %s AND id <> ALL(%s)", [source, id_list])
    # Tombstones for this source's ids it no longer lists are dead weight (issue #442) - only
    # the OSM loader passes OSM ids, so the prefix keeps another source from clearing them.
    con.execute("DELETE FROM trail_duplicates WHERE osm_id LIKE %s AND osm_id <> ALL(%s)", [f"{source}:%", id_list])
    con.commit()
    if result.rowcount:
        _invalidate_rank_cache()
    return result.rowcount
