"""SQL fragments and the genus-name lookup shared across the scoring package.

``regions``, ``ranking`` and ``queries`` all build queries over ``observations`` with the
same research-grade filter, the same decoy-aware center expressions, and the same
``taxon_id`` / ``IN (...)`` fragment helpers. They live here so the three modules can share
them without importing each other.
"""

from __future__ import annotations

from collections.abc import Collection

import psycopg

# The "research-grade only" invariant (see AGENTS.md) is enforced here, centrally, rather
# than trusted from the iNat API query param it started as (inat.py) - any row that lands in
# `observations` some other way (a future bulk loader, a manual insert) still can't leak into
# scoring.
# Explicit column list rather than `o.*`: the binned subquery feeds the big `GROUP BY`s in
# `regions` / `ranking` / `queries`, and `SELECT o.*` drags every observation column (incl.
# `positional_accuracy`, `revalidated_at`, and the future PostGIS `geom`) through each one. This
# is every column the aggregates actually read - keep it in sync when a query starts consuming a
# new one.
BINNED = """
SELECT
    o.id, o.taxon_id, o.lat, o.lng, o.observed_on, o.month, o.quality_grade,
    o.obscured, o.place_guess, o.uri, o.elevation_m, o.precip_7d_mm, o.precip_30d_mm,
    CAST(floor(o.lat / {cell}) AS INTEGER) AS ilat,
    CAST(floor(o.lng / {cell}) AS INTEGER) AS ilng,
    (CAST(floor(o.lat / {cell}) AS INTEGER))::text || '_' ||
        (CAST(floor(o.lng / {cell}) AS INTEGER))::text AS region_id
FROM observations o
WHERE o.quality_grade = 'research'
"""

# A geoprivacy-obscured observation's cached point is iNat's randomized decoy coordinate, not
# the true find location (see TODO.md's obscured-coordinate investigation) - averaging it in
# alongside precise points pulls a region's displayed center off target. Excludes obscured rows
# from the average when at least one precise point exists in the group; falls back to including
# them only when every row in the group is obscured, so the average is never NULL.
CENTER_LAT = "COALESCE(AVG(lat) FILTER (WHERE NOT COALESCE(obscured, false)), AVG(lat))::double precision"
CENTER_LNG = "COALESCE(AVG(lng) FILTER (WHERE NOT COALESCE(obscured, false)), AVG(lng))::double precision"


def sql_in(ids: list[int]) -> str:
    """SQL fragment for ``IN (...)``. An empty list becomes the literal ``NULL`` (matches
    nothing, valid SQL) rather than an empty ``IN ()``, which Postgres rejects as a syntax
    error - e.g. when ``months`` is empty.
    """
    return ",".join("%s" for _ in ids) if ids else "NULL"


def taxon_filter(taxon_ids: list[int], column: str = "taxon_id") -> str:
    """SQL condition for a ``taxon_id`` restriction. An empty list means "no genera selected"
    (issue #79 Phase 2) - unlike ``sql_in()``, that must mean "no filter" (``TRUE``, match
    every taxon), not "match nothing", so a fresh device with no selection sees everything
    nearby instead of an empty result.
    """
    return f"{column} IN ({sql_in(taxon_ids)})" if taxon_ids else "TRUE"


def genus_name_map(con: psycopg.Connection, taxon_ids: Collection[int]) -> dict[int, tuple[str, str | None]]:
    """taxon_id -> (scientific name, common name or None) for the given taxon_ids only.

    ``name`` is the primary display label (every ~6,018-genus catalog row has one);
    ``common_name`` is optional secondary enrichment - most genera outside the old curated
    21 lack an English common name on iNat (see fungi_genera's schema comment). Scoped to
    ``taxon_ids`` (rather than the full catalog) since callers only ever look up the handful
    of taxa present in their own result rows - fetching all ~6,018 rows on every request
    doesn't scale as the catalog grows.
    """
    if not taxon_ids:
        return {}
    rows = con.execute(
        "SELECT taxon_id, name, common_name FROM fungi_genera WHERE taxon_id = ANY(%s)",
        [list(taxon_ids)],
    ).fetchall()
    return {taxon_id: (name, common_name) for taxon_id, name, common_name in rows}
