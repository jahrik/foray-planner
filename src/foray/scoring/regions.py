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

from foray.scoring._sql import BINNED, CENTER_LAT, CENTER_LNG, taxon_filter

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

    Wrapped in one transaction (the connection otherwise runs autocommit) so a concurrent
    reader never sees a mid-rebuild state where the tables are dropped but not yet
    recreated.
    """
    binned = BINNED.format(cell=cell_deg)
    with con.transaction():
        con.execute("DROP TABLE IF EXISTS phenology")
        con.execute("DROP TABLE IF EXISTS regions")
        con.execute(
            cast(
                LiteralString,
                f"""
                CREATE TABLE phenology AS
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
                CREATE TABLE regions AS
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
        # rank_destinations filters/groups by (taxon_id, region_id); place_calendar filters by
        # region_id + taxon_id IN (...) - both scan the whole table without these, and
        # `phenology` scales with taxon x region x month so that gets expensive as
        # observations grow.
        con.execute("CREATE INDEX ix_phenology_taxon_region ON phenology (taxon_id, region_id)")
        con.execute("CREATE INDEX ix_phenology_region ON phenology (region_id)")
        # _rank_candidates joins `regions` back by region_id to attach each card's mean elevation.
        con.execute("CREATE INDEX ix_regions_region ON regions (region_id)")
        # Fresh tables have no planner statistics until autovacuum gets to them - without this,
        # requests right after a refresh could still get seq-scan plans despite the indexes above.
        con.execute("ANALYZE phenology")
        con.execute("ANALYZE regions")


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


def recent_counts(con: psycopg.Connection, cell_deg: float, taxon_ids: list[int], weeks: int) -> dict[str, int]:
    cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    binned = BINNED.format(cell=cell_deg)
    # cast: the query is built from a fixed template + `sql_in()`'s placeholder-count text
    # (never user data), but psycopg's LiteralString typing can't verify that statically.
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            SELECT region_id, count(*) AS cnt
            FROM ({binned})
            WHERE observed_on >= %s AND {taxon_filter(taxon_ids)}
            GROUP BY region_id
            """,
        ),
        [cutoff, *taxon_ids],
    ).fetchall()
    return dict(rows)
