"""Campsite (developed campground + OSM dispersed-site) table writes."""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from typing import Any, LiteralString

import psycopg

from foray.cache.core import _invalidate_rank_cache, upsert_rows
from foray.cache.land_trails import normalised_name_sql

logger = logging.getLogger(__name__)


def upsert_campsites(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert campsite tuples, refreshing existing rows in place. Returns rows attempted.

    Each tuple is (id, name, kind, fee, free, lat, lng, source, url, reservable, fee_low, fee_high,
    camp_type); a 12-tuple (no ``camp_type``) is read as ``camp_type = None``. Rows a dedup prune
    already folded away (a ``campsite_duplicates`` tombstone, issue #451) are dropped - every OSM
    reload still lists them, so without this each ingest would put the same pitch back.
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
        "camp_type",
    )
    rows = [(*row, None) if len(row) == len(columns) - 1 else row for row in rows]
    folded = _folded_ids(con, [row[0] for row in rows])
    rows = [row for row in rows if row[0] not in folded]
    # camp_type is coalesced: a RIDB row's merged-in OSM type survives a reload that has none.
    result = upsert_rows(con, "campsites", columns, rows, coalesce=("camp_type",))
    _invalidate_rank_cache()
    return result


def _folded_ids(con: psycopg.Connection, ids: Sequence[str]) -> set[str]:
    """The ``ids`` a ``campsite_duplicates`` tombstone currently keeps out of ``campsites``."""
    if not ids:
        return set()
    found = con.execute("SELECT dropped_id FROM campsite_duplicates WHERE dropped_id = ANY(%s)", [list(ids)])
    return {row[0] for row in found.fetchall()}


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


# --- Dedup (issue #451) ---
#
# Two rows are one campground when they sit within this of each other: an OSM campground with 80
# mapped pitches, or the same campground mapped in OSM and listed by Recreation.gov (measured on
# prod: the OSM~RIDB twins sit 30-80 m apart).
_CAMP_RADIUS_M = 150.0
# Words that don't tell two campground names apart ("Woods Lake Campground" / "Woods Lake").
_GENERIC_CAMP_WORDS = (
    "campground",
    "campgrounds",
    "camp",
    "camping",
    "campsite",
    "campsites",
    "site",
    "sites",
    "area",
    "recreation",
    "rv",
    "group",
    "the",
    "and",
    "of",
)
_GENERIC_CAMP_REGEX = r"\m(" + "|".join(_GENERIC_CAMP_WORDS) + r")\M"
# pg_trgm similarity at or above which two normalised names are the same campground ("margies
# cove" vs "margies cove west" scores 0.72, "woods lake" vs "snow lake" 0.31). Whole-string, not
# `word_similarity`: that scores a short name against any longer one containing its words, and
# camp names are short ("snow lake" vs "woods lake" is 0.5 by it).
_CAMP_NAME_SIMILARITY = 0.5
_PRUNE_TILE_DEG = 2.0
PITCH = "pitch"


def prune_duplicate_campsites(
    con: psycopg.Connection, *, min_lat: float, min_lng: float, max_lat: float, max_lng: float
) -> int:
    """Fold duplicate campsite rows whose point lies in this bbox. Returns rows removed.

    Three passes, each leaving a ``campsite_duplicates`` tombstone so the next ingest does not
    re-insert what it removed (every OSM reload still lists it):

    1. An OSM ``camp_pitch`` (``camp_type = 'pitch'``) folds into the nearest non-pitch campsite
       within ``_CAMP_RADIUS_M`` - the OSM campground it sits in, or a RIDB facility - and shows
       there as a pitch count instead of one dot per pitch.
    2. An OSM campground within ``_CAMP_RADIUS_M`` of a RIDB facility with a matching name (or no
       real name) folds into the RIDB row, which keeps the fee and reservation link; the OSM id is
       attached as ``osm_id``. RIDB~RIDB pairs (loops, group areas) are left alone: they are
       separate facilities as often as not.
    3. Pitches no campground claims fold into one row per cluster of pitches within
       ``_CAMP_RADIUS_M`` of each other.

    Scoped to one bbox, like ``prune_trail_duplicates``: callers pass the area they just wrote
    (an ingest tile) or a ``_PRUNE_TILE_DEG`` tile (:func:`prune_duplicate_campsites_tiled`)."""
    bounds = {"min_lat": min_lat, "min_lng": min_lng, "max_lat": max_lat, "max_lng": max_lng}
    folded_into: set[str] = set()
    total = 0
    for fold in (_fold_pitches_into_sites, _fold_osm_into_ridb, _fold_pitch_clusters):
        removed, parents = fold(con, **bounds)
        total += removed
        folded_into |= parents
    if folded_into:
        _recount_pitches(con, sorted(folded_into))
    con.commit()
    if total:
        _invalidate_rank_cache()
    return total


_ENVELOPE: LiteralString = "ST_MakeEnvelope(%s, %s, %s, %s, 4326)::geography"


def _fold(con: psycopg.Connection, pairs: Sequence[tuple[str, str]], reason: str) -> int:
    """Tombstone and delete each ``(dropped_id, kept_id)`` pair. Tombstones that pointed at a
    dropped row move to its replacement first - the ``kept_id`` foreign key would otherwise
    cascade them away with the delete and release every pitch folded into it."""
    if not pairs:
        return 0
    dropped = [dropped_id for dropped_id, _ in pairs]
    kept = [kept_id for _, kept_id in pairs]
    con.execute(
        "UPDATE campsite_duplicates d SET kept_id = swap.kept_id "
        "FROM unnest(%s::text[], %s::text[]) AS swap(dropped_id, kept_id) WHERE d.kept_id = swap.dropped_id",
        [dropped, kept],
    )
    con.execute(
        "INSERT INTO campsite_duplicates (dropped_id, kept_id, reason) "
        "SELECT dropped_id, kept_id, %s FROM unnest(%s::text[], %s::text[]) AS pair(dropped_id, kept_id) "
        "ON CONFLICT (dropped_id) DO UPDATE SET kept_id = EXCLUDED.kept_id, reason = EXCLUDED.reason",
        [reason, dropped, kept],
    )
    con.execute("DELETE FROM campsites WHERE id = ANY(%s)", [dropped])
    return len(dropped)


def _fold_pitches_into_sites(
    con: psycopg.Connection, *, min_lat: float, min_lng: float, max_lat: float, max_lng: float
) -> tuple[int, set[str]]:
    pairs = con.execute(
        f"""
        SELECT pitch.id, parent.id
        FROM campsites pitch
        CROSS JOIN LATERAL (
            SELECT site.id
            FROM campsites site
            WHERE site.camp_type IS DISTINCT FROM %s AND site.id <> pitch.id
              AND ST_DWithin(site.geom, pitch.geom, %s)
            ORDER BY ST_Distance(site.geom, pitch.geom), site.id
            LIMIT 1
        ) parent
        WHERE pitch.camp_type = %s AND pitch.geom && {_ENVELOPE}
        """,
        [PITCH, _CAMP_RADIUS_M, PITCH, min_lng, min_lat, max_lng, max_lat],
    ).fetchall()
    return _fold(con, pairs, PITCH), {parent_id for _, parent_id in pairs}


def _fold_osm_into_ridb(
    con: psycopg.Connection, *, min_lat: float, min_lng: float, max_lat: float, max_lng: float
) -> tuple[int, set[str]]:
    name_match: LiteralString = (
        # An OSM site with no real name of its own ("Campsite (OSM)") counts as a match on
        # location alone - but a backcountry site is a place of its own, never a campground twin.
        "(osm.name LIKE '%%(OSM)' AND osm.camp_type IS DISTINCT FROM 'backcountry') "
        "OR (osm.name NOT LIKE '%%(OSM)' AND "
        "EXISTS (SELECT 1 FROM (SELECT "
        + normalised_name_sql("osm.name")
        + " AS osm_name, "
        + normalised_name_sql("ridb.name")
        + " AS ridb_name) names WHERE osm_name <> '' AND ridb_name <> '' "
        "AND similarity(osm_name, ridb_name) >= %s))"
    )
    pairs = con.execute(
        f"""
        SELECT DISTINCT ON (osm.id) osm.id, ridb.id
        FROM campsites osm
        JOIN campsites ridb ON ridb.source = 'ridb' AND ST_DWithin(ridb.geom, osm.geom, %s)
        WHERE osm.source = 'osm' AND osm.camp_type IS DISTINCT FROM %s
          AND osm.geom && {_ENVELOPE}
          AND ({name_match})
        ORDER BY osm.id, ST_Distance(ridb.geom, osm.geom), ridb.id
        """,
        [
            _CAMP_RADIUS_M,
            PITCH,
            min_lng,
            min_lat,
            max_lng,
            max_lat,
            _GENERIC_CAMP_REGEX,
            _GENERIC_CAMP_REGEX,
            _CAMP_NAME_SIMILARITY,
        ],
    ).fetchall()
    if not pairs:
        return 0, set()
    # The surviving RIDB row learns its twin's id and, when RIDB gave it no type, the OSM tags' type.
    con.execute(
        """
        UPDATE campsites ridb
        SET osm_id = coalesce(ridb.osm_id, twin.osm_id),
            camp_type = coalesce(ridb.camp_type, twin.camp_type)
        FROM (
            SELECT DISTINCT ON (pair.ridb_id) pair.ridb_id, pair.osm_id, o.camp_type
            FROM unnest(%s::text[], %s::text[]) AS pair(osm_id, ridb_id)
            JOIN campsites o ON o.id = pair.osm_id
            ORDER BY pair.ridb_id, pair.osm_id
        ) twin
        WHERE ridb.id = twin.ridb_id
        """,
        [[osm_id for osm_id, _ in pairs], [ridb_id for _, ridb_id in pairs]],
    )
    return _fold(con, pairs, "osm-twin"), {ridb_id for _, ridb_id in pairs}


def _fold_pitch_clusters(
    con: psycopg.Connection, *, min_lat: float, min_lng: float, max_lat: float, max_lng: float
) -> tuple[int, set[str]]:
    # DBSCAN in Web Mercator, whose distances are the ground distance over cos(latitude): the
    # tile's middle latitude sets one eps for the whole bbox (a 2-degree tile is within ~3%).
    eps = _CAMP_RADIUS_M / max(math.cos(math.radians((min_lat + max_lat) / 2)), 0.05)
    rows = con.execute(
        f"""
        SELECT id, cluster FROM (
            SELECT id, ST_ClusterDBSCAN(ST_Transform(geom::geometry, 3857), %s, 1) OVER () AS cluster
            FROM campsites
            WHERE camp_type = %s AND geom && {_ENVELOPE}
        ) clustered
        """,
        [eps, PITCH, min_lng, min_lat, max_lng, max_lat],
    ).fetchall()
    members: dict[int, list[str]] = {}
    for campsite_id, cluster in rows:
        members.setdefault(cluster, []).append(campsite_id)
    pairs: list[tuple[str, str]] = []
    for ids in members.values():
        ids.sort()
        pairs.extend((dropped_id, ids[0]) for dropped_id in ids[1:])
    return _fold(con, pairs, PITCH), {kept_id for _, kept_id in pairs}


def _recount_pitches(con: psycopg.Connection, kept_ids: Sequence[str]) -> None:
    """``pitch_count`` = the pitches folded into each row (+1 when the row is itself a pitch)."""
    con.execute(
        """
        UPDATE campsites c
        SET pitch_count = counted.folded + CASE WHEN c.camp_type = %s THEN 1 ELSE 0 END
        FROM (
            SELECT kept_id, count(*) AS folded FROM campsite_duplicates
            WHERE reason = %s AND kept_id = ANY(%s) GROUP BY kept_id
        ) counted
        WHERE c.id = counted.kept_id
        """,
        [PITCH, PITCH, list(kept_ids)],
    )


def prune_duplicate_campsites_tiled(
    con: psycopg.Connection, bounds: tuple[float, float, float, float] | None = None
) -> int:
    """:func:`prune_duplicate_campsites` over every ``_PRUNE_TILE_DEG`` tile that holds a campsite
    (inside ``bounds`` = ``(min_lat, min_lng, max_lat, max_lng)`` when given) - what the RIDB bulk
    load, the OSM ingests and ``foray prune-duplicates`` call, since a newly loaded row can twin
    one cached long before. Tile by tile, each its own committed statement."""
    box: dict[str, float] = {}
    if bounds is not None:
        box = dict(zip(("min_lat", "min_lng", "max_lat", "max_lng"), bounds, strict=True))
    cells = con.execute(
        "SELECT DISTINCT floor(lat / %(tile)s)::int, floor(lng / %(tile)s)::int FROM campsites "
        "WHERE geom IS NOT NULL AND (%(min_lat)s::float8 IS NULL OR (lat BETWEEN %(min_lat)s AND %(max_lat)s "
        "AND lng BETWEEN %(min_lng)s AND %(max_lng)s)) ORDER BY 1, 2",
        {"tile": _PRUNE_TILE_DEG, "min_lat": None, "min_lng": None, "max_lat": None, "max_lng": None, **box},
    ).fetchall()
    total = 0
    for lat_cell, lng_cell in cells:
        south, west = lat_cell * _PRUNE_TILE_DEG, lng_cell * _PRUNE_TILE_DEG
        total += prune_duplicate_campsites(
            con, min_lat=south, min_lng=west, max_lat=south + _PRUNE_TILE_DEG, max_lng=west + _PRUNE_TILE_DEG
        )
    logger.info("campsites: folded %d duplicate rows across %d tiles", total, len(cells))
    return total


def upsert_campsites_deduped(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """:func:`upsert_campsites`, then fold the duplicates the new rows can have created - pitches
    into their campground, an OSM site into the RIDB twin. What every campsite ingest writes
    through, so the cache never holds a duplicate longer than one ingest call."""
    result = upsert_campsites(con, rows)
    if rows:
        lats = [row[5] for row in rows]
        lngs = [row[6] for row in rows]
        # Padded past the dedup radius: a row at the edge may twin one just outside it.
        pad = 0.01
        prune_duplicate_campsites_tiled(con, (min(lats) - pad, min(lngs) - pad, max(lats) + pad, max(lngs) + pad))
    return result
