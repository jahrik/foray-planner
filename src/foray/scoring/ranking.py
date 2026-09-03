"""The three region-ranking modes built on the materialized ``phenology`` table.

All scoring is built from three primitives per (taxon, region, month):

* **w_pheno** - share of that taxon's regional observations that fall in the target
  month(s): "is it in season here?" (0..1)
* **abundance** - log-scaled observation count: "how reliably does it show up here?"
* **recency** - this-year observations in a trailing window: "is it going off now?"

Fix the month axis -> rank regions (``rank_destinations`` / ``rank_destinations_corridor``).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from dataclasses import replace
from typing import Any, LiteralString, cast

import psycopg

from foray.cache import region_precip
from foray.geo import (
    bbox_around,
    bbox_around_segment,
    bbox_center_radius,
    grid_cells_in_bbox,
    haversine_km,
    project_to_plane,
    segment_progress_and_offset,
)
from foray.scoring._sql import genus_name_map, sql_in, taxon_filter
from foray.scoring.models import FireNear, RegionScore, SpeciesHit
from foray.scoring.queries import fire_near
from foray.scoring.regions import recent_counts, region_elevations, region_precip_obs

# --- Fire scoring inputs (issue #227) -------------------------------------------------------
# Conservative starting weights - the plan is to eyeball real fire numbers on the map for a few
# weeks before tuning, same approach as elevation #36 / rain #226. All multiplicative on a
# region's raw score, applied after the phenology ranking.
FIRE_PENALTY_RADIUS_KM = 25.0  # an active perimeter within this of a region center -> penalty
FIRE_PENALTY_FACTOR = 0.35  # raw score is multiplied by this (safety/access: you can't forage there)
BURN_SCAR_BOOST_RADIUS_KM = 30.0  # a qualifying burn scar within this -> Morchella boost
BURN_SCAR_BOOST_YEAR1 = 1.6  # year-1 scar (heaviest burn-morel flush)
BURN_SCAR_BOOST_YEAR2 = 1.25  # year-2 scar (still good)
# High-severity scars are poor morel producers - low/moderate/unknown severity only.
_BOOSTABLE_SEVERITY = {None, "low", "moderate"}


def _rank_candidates(
    con: psycopg.Connection,
    *,
    months: list[int],
    taxon_ids: list[int],
    cell_deg: float,
    recent_weeks: int,
    region_ids: list[str],
    recent_center: tuple[float, float],
    recent_radius_km: float,
    keep: Callable[[float, float], tuple[bool, float]],
) -> list[RegionScore]:
    """Shared fetch/score core for ``rank_destinations``/``rank_destinations_corridor``.

    ``region_ids`` is the candidate grid-cell allowlist (a bounding box over the home
    radius / corridor, from :func:`grid_cells_in_bbox`); the phenology scan is restricted
    to it via ``ix_phenology_region`` instead of aggregating every ingested cell globally.
    ``recent_center`` / ``recent_radius_km`` bound the ``recent_counts`` observation scan the
    same way (a circle enclosing the candidate area). ``keep(center_lat, center_lng)`` then
    does the exact radius / corridor-offset test on that already-small set and supplies the
    value that lands in ``RegionScore.distance_km`` - home-distance for the radial caller,
    progress-along-line for the corridor caller. Everything else (genus lookup, recency, and
    score formula) is identical between modes.
    """
    if not region_ids:
        return []
    # Per (region, taxon): observations in the target months vs. all months, scoped to the
    # candidate cells so the double GROUP BY runs over ~hundreds of rows, not the whole table.
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
                WHERE {taxon_filter(taxon_ids)} AND region_id = ANY(%s)
                GROUP BY region_id, taxon_id
            ),
            win AS (
                SELECT region_id, taxon_id, sum(cnt)::bigint AS month_cnt
                FROM phenology
                WHERE {taxon_filter(taxon_ids)} AND region_id = ANY(%s)
                      AND month IN ({sql_in(months)})
                GROUP BY region_id, taxon_id
            )
            SELECT tot.region_id, tot.center_lat, tot.center_lng, tot.taxon_id,
                   COALESCE(win.month_cnt, 0) AS month_cnt, tot.total_cnt
            FROM tot LEFT JOIN win USING (region_id, taxon_id)
            WHERE COALESCE(win.month_cnt, 0) > 0
            """,
        ),
        [*taxon_ids, region_ids, *taxon_ids, region_ids, *months],
    ).fetchall()

    genera = genus_name_map(con, {row[3] for row in rows})
    recent = recent_counts(
        con,
        lat=recent_center[0],
        lng=recent_center[1],
        radius_km=recent_radius_km,
        cell_deg=cell_deg,
        taxon_ids=taxon_ids,
        weeks=recent_weeks,
    )

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
    precip_obs = region_precip_obs(con, regions.keys())
    recent_rain = region_precip(con, regions.keys())

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
                precip_obs_7d_mm=precip_obs.get(region_id, {}).get("precip_obs_7d_mm"),
                precip_obs_30d_mm=precip_obs.get(region_id, {}).get("precip_obs_30d_mm"),
                precip_recent_7d_mm=recent_rain.get(region_id, {}).get("precip_7d_mm"),
                precip_recent_14d_mm=recent_rain.get(region_id, {}).get("precip_14d_mm"),
                precip_recent_30d_mm=recent_rain.get(region_id, {}).get("precip_30d_mm"),
            )
        )

    top_score = max((region.score for region in results), default=0.0)
    for region in results:
        region.score_norm = round(region.score / top_score, 4) if top_score else 0.0
    results.sort(key=lambda region: region.score, reverse=True)
    return results


def _morchella_targeted(con: psycopg.Connection, taxon_ids: list[int]) -> bool:
    """True when the device has explicitly selected genus *Morchella* (issue #227's burn-scar
    boost is opt-in - mirrors how `/api/trees` scopes to selected ECM genera). An empty
    ``taxon_ids`` ("everything nearby") is not an explicit selection, so it does not qualify."""
    if not taxon_ids:
        return False
    row = con.execute("SELECT taxon_id FROM fungi_genera WHERE lower(name) = 'morchella'").fetchone()
    return row is not None and row[0] in taxon_ids


def _apply_fire(con: psycopg.Connection, results: list[RegionScore], *, taxon_ids: list[int]) -> None:
    """Fold the fire signals (issue #227) into an already-ranked region list, in place:

    * a **penalty** on any region with an active perimeter within ``FIRE_PENALTY_RADIUS_KM``
      (safety/access - you can't forage in an active fire area);
    * a **boost** on any region near a low/moderate/unknown-severity year-1 or year-2 burn
      scar, but only when the device targets *Morchella*.

    Also attaches the nearby fires/scars to ``region.fire_nearby`` for the card annotation.
    Re-normalizes ``score_norm`` and re-sorts. A no-op when nothing is ingested / in range."""
    if not results:
        return
    lats = [region.center_lat for region in results]
    lngs = [region.center_lng for region in results]
    mid_lat, mid_lng = sum(lats) / len(lats), sum(lngs) / len(lngs)
    span_km = haversine_km(min(lats), min(lngs), max(lats), max(lngs))
    fires = fire_near(
        con,
        lat=mid_lat,
        lng=mid_lng,
        radius_km=span_km / 2 + BURN_SCAR_BOOST_RADIUS_KM + FIRE_PENALTY_RADIUS_KM,
    )
    if not fires:
        return
    boost_ok = _morchella_targeted(con, taxon_ids)
    for region in results:
        nearby: list[FireNear] = []
        penalty = False
        boost = 1.0
        for fire in fires:
            gap = haversine_km(region.center_lat, region.center_lng, fire.center_lat, fire.center_lng)
            # `fires` carries distance_km relative to the fetch centroid - copy each hit with
            # the distance to *this* region so cards/plan-stop warnings show the right number.
            local = replace(fire, distance_km=round(gap, 1))
            if fire.status == "active" and gap <= FIRE_PENALTY_RADIUS_KM:
                penalty = True
                nearby.append(local)
            elif (
                fire.status == "historical"
                and gap <= BURN_SCAR_BOOST_RADIUS_KM
                and fire.dominant_severity in _BOOSTABLE_SEVERITY
                and fire.fire_year is not None
            ):
                nearby.append(local)
                if boost_ok:
                    age = dt.date.today().year - fire.fire_year
                    boost = max(
                        boost, BURN_SCAR_BOOST_YEAR1 if age <= 1 else BURN_SCAR_BOOST_YEAR2 if age == 2 else 1.0
                    )
        if penalty:
            region.score *= FIRE_PENALTY_FACTOR
        region.score *= boost
        region.fire_nearby = sorted(nearby, key=lambda item: item.distance_km)[:5]

    top_score = max((region.score for region in results), default=0.0)
    for region in results:
        region.score_norm = round(region.score / top_score, 4) if top_score else 0.0
    results.sort(key=lambda region: region.score, reverse=True)


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

    region_ids = grid_cells_in_bbox(bbox_around(home_lat, home_lng, radius_km), cell_deg)
    results = _rank_candidates(
        con,
        months=months,
        taxon_ids=taxon_ids,
        cell_deg=cell_deg,
        recent_weeks=recent_weeks,
        region_ids=region_ids,
        recent_center=(home_lat, home_lng),
        recent_radius_km=radius_km,
        keep=keep,
    )
    _apply_fire(con, results, taxon_ids=taxon_ids)
    return results


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

    corridor_bbox = bbox_around_segment(start_lat, start_lng, dest_lat, dest_lng, corridor_km)
    region_ids = grid_cells_in_bbox(corridor_bbox, cell_deg)
    # recent_counts only needs a superset of the candidate area (see _rank_candidates); the
    # circumscribed circle of the same bounding box is the tightest such circle, and keeps the
    # ST_DWithin scan from ballooning on a long corridor the way a start-anchored radius would.
    recent_lat, recent_lng, recent_radius_km = bbox_center_radius(corridor_bbox)
    results = _rank_candidates(
        con,
        months=months,
        taxon_ids=taxon_ids,
        cell_deg=cell_deg,
        recent_weeks=recent_weeks,
        region_ids=region_ids,
        recent_center=(recent_lat, recent_lng),
        recent_radius_km=recent_radius_km,
        keep=keep,
    )
    _apply_fire(con, results, taxon_ids=taxon_ids)
    return results
