"""Phenology materialization: (re)build the ``regions`` and ``phenology`` tables.

Regions are uniform lat/lng grid cells (``cell_deg`` wide). ``build_phenology`` rolls
``observations`` up into per-(taxon, region, month) counts (``phenology``) and per-region
summaries (``regions``); everything in ``ranking`` / ``queries`` reads those.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection
from typing import LiteralString, cast

import psycopg

from foray.scoring._sql import BINNED, CENTER_LAT, CENTER_LNG, GEOG_POINT, taxon_filter

# Mean ground elevation for a region (issue #36), over the observations that have one - obscured
# rows are excluded (their point is iNat's decoy, so its elevation is meaningless) unless every
# row in the group is obscured, matching CENTER_LAT. NULL when no observation in the region has
# been elevation-enriched yet (see ingest.backfill_elevations).
_ELEVATION = "ROUND(COALESCE(AVG(elevation_m) FILTER (WHERE NOT COALESCE(obscured, false)), AVG(elevation_m)))::int"

# Mean antecedent rainfall over the region's enriched observations (issue #226), decoy-excluded
# the same way as _ELEVATION. NULL until at least one observation in the region has been
# precip-enriched (see ingest.backfill_precip). Rounded to 0.1 mm.
_PRECIP_OBS_7D = (
    "ROUND(COALESCE(AVG(precip_7d_mm) FILTER (WHERE NOT COALESCE(obscured, false)),"
    " AVG(precip_7d_mm))::numeric, 1)::double precision"
)
_PRECIP_OBS_30D = (
    "ROUND(COALESCE(AVG(precip_30d_mm) FILTER (WHERE NOT COALESCE(obscured, false)),"
    " AVG(precip_30d_mm))::numeric, 1)::double precision"
)


def build_phenology(con: psycopg.Connection, cell_deg: float) -> None:
    """(Re)materialize the ``regions`` and ``phenology`` tables from ``observations``.

    Build-and-swap: the scan + aggregate + index builds + ``ANALYZE`` (the slow part, and the
    heaviest single op in the app on the 1-vCPU box) run against ``*_new`` staging tables
    *outside* any transaction, so the live ``regions`` / ``phenology`` keep serving reads the
    whole time. Only the cutover - drop the old tables, rename the new ones into place - runs
    in a transaction, and readers block just for that millisecond-scale window.

    End state is byte-for-byte the previous layout: tables ``regions`` / ``phenology`` with
    their normal index names. ``*_new`` is a transient staging name; a crash mid-rebuild
    leaves a stray ``phenology_new`` (dropped at the top of the next run) rather than a lock
    held for the whole rebuild or half-built live tables.
    """
    binned = BINNED.format(cell=cell_deg)
    # A previous crash between the CREATE and the cutover can leave staging tables behind.
    con.execute("DROP TABLE IF EXISTS phenology_new, regions_new")
    con.execute(
        cast(
            LiteralString,
            f"""
            CREATE TABLE phenology_new AS
            SELECT region_id,
                   {CENTER_LAT} AS center_lat,
                   {CENTER_LNG} AS center_lng,
                   taxon_id, month, count(*) AS cnt
            FROM ({binned})
            GROUP BY region_id, taxon_id, month
            """,
        )
    )
    con.execute(
        cast(
            LiteralString,
            f"""
            CREATE TABLE regions_new AS
            SELECT region_id,
                   {CENTER_LAT} AS center_lat,
                   {CENTER_LNG} AS center_lng,
                   {_ELEVATION} AS elevation_m,
                   {_PRECIP_OBS_7D} AS precip_obs_7d_mm,
                   {_PRECIP_OBS_30D} AS precip_obs_30d_mm,
                   count(*) AS n_obs,
                   count(DISTINCT taxon_id) AS n_taxa
            FROM ({binned})
            GROUP BY region_id
            """,
        )
    )
    # `_new`-suffixed so they don't collide with the live indexes while both tables exist;
    # renamed to their normal names in the cutover. rank_destinations filters/groups by
    # (taxon_id, region_id); place_calendar filters by region_id + taxon_id IN (...) - both
    # scan the whole table without these, and `phenology` scales with taxon x region x month.
    con.execute("CREATE INDEX ix_phenology_taxon_region_new ON phenology_new (taxon_id, region_id)")
    con.execute("CREATE INDEX ix_phenology_region_new ON phenology_new (region_id)")
    # _rank_candidates joins `regions` back by region_id to attach each card's mean elevation.
    con.execute("CREATE INDEX ix_regions_region_new ON regions_new (region_id)")
    # Fresh tables have no planner statistics until autovacuum gets to them - without this,
    # requests right after the swap could still get seq-scan plans despite the indexes above.
    # ANALYZE now so the fresh stats ride along with the rename.
    con.execute("ANALYZE phenology_new")
    con.execute("ANALYZE regions_new")
    # Cutover: readers block only here. DROP ... IF EXISTS covers the first-ever build.
    with con.transaction():
        con.execute("DROP TABLE IF EXISTS phenology")
        con.execute("DROP TABLE IF EXISTS regions")
        con.execute("ALTER TABLE phenology_new RENAME TO phenology")
        con.execute("ALTER TABLE regions_new RENAME TO regions")
        con.execute("ALTER INDEX ix_phenology_taxon_region_new RENAME TO ix_phenology_taxon_region")
        con.execute("ALTER INDEX ix_phenology_region_new RENAME TO ix_phenology_region")
        con.execute("ALTER INDEX ix_regions_region_new RENAME TO ix_regions_region")


def region_elevations(con: psycopg.Connection, region_ids: Collection[str]) -> dict[str, int | None]:
    """region_id -> mean ground elevation (metres), from the materialized `regions` table
    (issue #36). Absent/NULL when no observation in that region is elevation-enriched yet."""
    if not region_ids:
        return {}
    try:
        rows = con.execute(
            "SELECT region_id, elevation_m FROM regions WHERE region_id = ANY(%s)",
            [list(region_ids)],
        ).fetchall()
    except psycopg.errors.UndefinedColumn:
        # `regions` is materialized by build_phenology, not cache.SCHEMA, so a deploy that adds
        # a column lands before the next ingest/refresh rebuilds the table. Degrade to "no
        # elevation" rather than 500 the whole ranking; the next rebuild fills it in.
        con.rollback()
        return {}
    return dict(rows)


def region_precip_obs(con: psycopg.Connection, region_ids: Collection[str]) -> dict[str, dict[str, float | None]]:
    """``region_id -> {"precip_obs_7d_mm", "precip_obs_30d_mm"}``: the decoy-excluded mean
    antecedent rainfall over each region's enriched observations (issue #226), from the
    materialized ``regions`` table. Empty / all-NULL until ``backfill_precip`` has run."""
    if not region_ids:
        return {}
    try:
        rows = con.execute(
            "SELECT region_id, precip_obs_7d_mm, precip_obs_30d_mm FROM regions WHERE region_id = ANY(%s)",
            [list(region_ids)],
        ).fetchall()
    except psycopg.errors.UndefinedColumn:
        # `regions` is materialized by build_phenology, not cache.SCHEMA - a deploy that adds
        # these columns lands before the next rebuild. Degrade to "no rain readout" rather than
        # 500 the ranking; the next refresh fills it in. (Mirrors region_elevations.)
        con.rollback()
        return {}
    return {region_id: {"precip_obs_7d_mm": mm7, "precip_obs_30d_mm": mm30} for region_id, mm7, mm30 in rows}


def recent_counts(
    con: psycopg.Connection,
    *,
    lat: float,
    lng: float,
    radius_km: float,
    cell_deg: float,
    taxon_ids: list[int],
    weeks: int,
) -> dict[str, int]:
    """``region_id -> count`` of research-grade target-taxon observations in the trailing
    ``weeks``, within ``radius_km`` of ``(lat, lng)``.

    The radius cut is an index-backed ``ST_DWithin`` on ``observations.geom`` (issue #268) so
    this doesn't seq-scan the whole table on every ranking call - the caller only ever reads
    the counts for regions that already survived its spatial filter, so a superset of that
    area is all that's needed. ``region_id`` is the same ``floor(coord / cell_deg)`` grid key
    ``BINNED`` derives.
    """
    cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    region_id = (
        f"(CAST(floor(o.lat / {cell_deg}) AS INTEGER))::text || '_' || "
        f"(CAST(floor(o.lng / {cell_deg}) AS INTEGER))::text"
    )
    # cast: the query is a fixed template + `taxon_filter()`'s placeholder-count text and
    # `cell_deg` (a config float, never user data); psycopg's LiteralString typing can't
    # verify that statically.
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            WITH pt AS (SELECT {GEOG_POINT} AS g)
            SELECT {region_id} AS region_id, count(*) AS cnt
            FROM observations o, pt
            WHERE o.quality_grade = 'research'
              AND o.geom IS NOT NULL AND ST_DWithin(o.geom, pt.g, %s)
              AND o.observed_on >= %s AND {taxon_filter(taxon_ids, "o.taxon_id")}
            GROUP BY 1
            """,
        ),
        [lng, lat, radius_km * 1000.0, cutoff, *taxon_ids],
    ).fetchall()
    return dict(rows)
