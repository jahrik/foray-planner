"""The three region-ranking modes built on the materialized ``phenology`` table.

All scoring is built from three primitives per (taxon, region, month):

* **w_pheno** - share of that taxon's regional observations that fall in the target
  month(s): "is it in season here?" (0..1)
* **abundance** - log-scaled observation count: "how reliably does it show up here?"
* **recency** - this-year observations in a trailing window: "is it going off now?"

Fix the month axis -> rank regions (``rank_destinations`` / ``rank_destinations_corridor``).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, LiteralString, cast

import psycopg

from foray._scoring_sql import genus_name_map, sql_in, taxon_filter
from foray.geo import haversine_km, project_to_plane, segment_progress_and_offset
from foray.models import RegionScore, SpeciesHit
from foray.regions import recent_counts, region_elevations


def _rank_candidates(
    con: psycopg.Connection,
    *,
    months: list[int],
    taxon_ids: list[int],
    cell_deg: float,
    recent_weeks: int,
    keep: Callable[[float, float], tuple[bool, float]],
) -> list[RegionScore]:
    """Shared fetch/score core for ``rank_destinations``/``rank_destinations_corridor``.

    ``keep(center_lat, center_lng)`` decides whether a region survives and supplies the
    value that lands in ``RegionScore.distance_km`` - home-distance for the radial caller,
    progress-along-line for the corridor caller. Everything else (the SQL fetch, genus
    lookup, recency, and score formula) is identical between the two modes.
    """
    # Per (region, taxon): observations in the target months vs. all months.
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            WITH tot AS (
                SELECT region_id, taxon_id,
                       (sum(center_lat * cnt) / sum(cnt))::double precision AS center_lat,
                       (sum(center_lng * cnt) / sum(cnt))::double precision AS center_lng,
                       sum(cnt)::bigint AS total_cnt
                FROM phenology
                WHERE {taxon_filter(taxon_ids)}
                GROUP BY region_id, taxon_id
            ),
            win AS (
                SELECT region_id, taxon_id, sum(cnt)::bigint AS month_cnt
                FROM phenology
                WHERE {taxon_filter(taxon_ids)} AND month IN ({sql_in(months)})
                GROUP BY region_id, taxon_id
            )
            SELECT tot.region_id, tot.center_lat, tot.center_lng, tot.taxon_id,
                   COALESCE(win.month_cnt, 0) AS month_cnt, tot.total_cnt
            FROM tot LEFT JOIN win USING (region_id, taxon_id)
            WHERE COALESCE(win.month_cnt, 0) > 0
            """,
        ),
        [*taxon_ids, *taxon_ids, *months],
    ).fetchall()

    genera = genus_name_map(con, {row[3] for row in rows})
    recent = recent_counts(con, cell_deg, taxon_ids, recent_weeks)

    # Group per region, applying the caller's filter and the score formula.
    regions: dict[str, dict[str, Any]] = {}
    for region_id, clat, clng, taxon_id, month_cnt, total_cnt in rows:
        keep_it, dist = keep(clat, clng)
        if not keep_it:
            continue
        w_pheno = month_cnt / total_cnt if total_cnt else 0.0
        agg = regions.setdefault(
            region_id,
            {"clat": clat, "clng": clng, "dist": dist, "score": 0.0, "species": []},
        )
        agg["score"] += w_pheno * math.log1p(month_cnt)
        name, common_name = genera.get(taxon_id, (str(taxon_id), None))
        agg["species"].append(SpeciesHit(taxon_id, name, common_name, month_cnt, total_cnt, w_pheno))

    elevations = region_elevations(con, regions.keys())

    results: list[RegionScore] = []
    for region_id, agg in regions.items():
        n_species = len(agg["species"])
        recent_count = recent.get(region_id, 0)
        # Diversity bonus (more choice species in season) + live recency boost.
        raw = agg["score"] * (1 + 0.1 * (n_species - 1)) * (1 + math.log1p(recent_count))
        agg["species"].sort(key=lambda hit: hit.month_count, reverse=True)
        results.append(
            RegionScore(
                region_id=region_id,
                center_lat=agg["clat"],
                center_lng=agg["clng"],
                distance_km=round(agg["dist"], 1),
                score=raw,
                score_norm=0.0,
                n_species=n_species,
                recent_count=recent_count,
                species=agg["species"],
                elevation_m=elevations.get(region_id),
            )
        )

    top_score = max((region.score for region in results), default=0.0)
    for region in results:
        region.score_norm = round(region.score / top_score, 4) if top_score else 0.0
    results.sort(key=lambda region: region.score, reverse=True)
    return results


def rank_destinations(
    con: psycopg.Connection,
    *,
    months: list[int],
    taxon_ids: list[int],
    home_lat: float,
    home_lng: float,
    radius_km: float,
    cell_deg: float,
    recent_weeks: int = 4,
) -> list[RegionScore]:
    """Rank grid regions within radius by expected choice-fungi activity in ``months``.

    ``RegionScore.distance_km`` here is straight-line distance from ``(home_lat, home_lng)``.
    """

    def keep(clat: float, clng: float) -> tuple[bool, float]:
        dist = haversine_km(home_lat, home_lng, clat, clng)
        return dist <= radius_km, dist

    return _rank_candidates(
        con, months=months, taxon_ids=taxon_ids, cell_deg=cell_deg, recent_weeks=recent_weeks, keep=keep
    )


def rank_destinations_corridor(
    con: psycopg.Connection,
    *,
    months: list[int],
    taxon_ids: list[int],
    start_lat: float,
    start_lng: float,
    dest_lat: float,
    dest_lng: float,
    corridor_km: float,
    cell_deg: float,
    recent_weeks: int = 4,
) -> list[RegionScore]:
    """Rank grid regions within ``corridor_km`` of the straight line ``start`` -> ``dest``.

    ``RegionScore.distance_km`` here is repurposed as progress-along-line in km (0 at
    ``start``, ``haversine_km(start, dest)`` at ``dest``) rather than home-distance, so
    callers can order stops "along the way" with a plain sort. A degenerate segment
    (``dest`` ~= ``start``) falls back to plain radial distance from ``start`` as progress.
    """
    dx, dy = project_to_plane(start_lat, start_lng, dest_lat, dest_lng)
    total_km = haversine_km(start_lat, start_lng, dest_lat, dest_lng)

    def keep(clat: float, clng: float) -> tuple[bool, float]:
        px, py = project_to_plane(start_lat, start_lng, clat, clng)
        t, offset_km = segment_progress_and_offset(px, py, dx, dy)
        # segment_progress_and_offset's degenerate-segment branch always returns t=0.0, which
        # would make every candidate's progress 0 (collapsing the sort order) - use the radial
        # offset (= distance from start in that branch) as progress instead, matching the
        # docstring's fallback.
        progress_km = offset_km if total_km == 0 else t * total_km
        return offset_km <= corridor_km, progress_km

    return _rank_candidates(
        con, months=months, taxon_ids=taxon_ids, cell_deg=cell_deg, recent_weeks=recent_weeks, keep=keep
    )
