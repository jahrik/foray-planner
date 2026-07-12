"""Phenology materialization + the three scoring modes.

Regions are uniform lat/lng grid cells (``cell_deg`` wide). All scoring is built from
three primitives per (taxon, region, month):

* **w_pheno** — share of that taxon's regional observations that fall in the target
  month(s): "is it in season here?" (0..1)
* **abundance** — log-scaled observation count: "how reliably does it show up here?"
* **recency** — this-year observations in a trailing window: "is it going off now?"

Fix the month axis -> rank regions (destinations). Fix the region -> rank months
(calendar). Fix species + recency -> alerts.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

import duckdb

# Grid-cell id and center, derived from lat/lng and a cell size (degrees).
# Kept as a reusable SQL fragment so binning is defined once.
_BINNED = """
SELECT
    o.*,
    CAST(floor(o.lat / {cell}) AS INTEGER) AS ilat,
    CAST(floor(o.lng / {cell}) AS INTEGER) AS ilng,
    CAST((CAST(floor(o.lat / {cell}) AS INTEGER) + 0.5) * {cell} AS DOUBLE) AS center_lat,
    CAST((CAST(floor(o.lng / {cell}) AS INTEGER) + 0.5) * {cell} AS DOUBLE) AS center_lng,
    printf('%d_%d', CAST(floor(o.lat / {cell}) AS INTEGER),
                    CAST(floor(o.lng / {cell}) AS INTEGER)) AS region_id
FROM observations o
"""


def build_phenology(con: duckdb.DuckDBPyConnection, cell_deg: float) -> None:
    """(Re)materialize the ``regions`` and ``phenology`` tables from ``observations``."""
    binned = _BINNED.format(cell=cell_deg)
    con.execute("DROP TABLE IF EXISTS phenology")
    con.execute("DROP TABLE IF EXISTS regions")
    con.execute(
        f"""
        CREATE TABLE phenology AS
        SELECT region_id, center_lat, center_lng, taxon_id, month, count(*) AS cnt
        FROM ({binned})
        GROUP BY region_id, center_lat, center_lng, taxon_id, month
        """
    )
    con.execute(
        f"""
        CREATE TABLE regions AS
        SELECT region_id, center_lat, center_lng,
               count(*) AS n_obs,
               count(DISTINCT taxon_id) AS n_taxa
        FROM ({binned})
        GROUP BY region_id, center_lat, center_lng
        """
    )


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    earth_radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    inner = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * earth_radius_km * math.asin(math.sqrt(inner))


@dataclass
class SpeciesHit:
    taxon_id: int
    common_name: str
    month_count: int
    total_count: int
    w_pheno: float


@dataclass
class RegionScore:
    region_id: str
    center_lat: float
    center_lng: float
    distance_km: float
    score: float
    score_norm: float
    n_species: int
    recent_count: int
    species: list[SpeciesHit]


def _recent_counts(
    con: duckdb.DuckDBPyConnection, cell_deg: float, taxon_ids: list[int], weeks: int
) -> dict[str, int]:
    cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    binned = _BINNED.format(cell=cell_deg)
    rows = con.execute(
        f"""
        SELECT region_id, count(*) AS cnt
        FROM ({binned})
        WHERE observed_on >= ? AND taxon_id IN ({_in(taxon_ids)})
        GROUP BY region_id
        """,
        [cutoff, *taxon_ids],
    ).fetchall()
    return dict(rows)


def _in(ids: list[int]) -> str:
    return ",".join("?" for _ in ids)


def rank_destinations(
    con: duckdb.DuckDBPyConnection,
    *,
    months: list[int],
    taxon_ids: list[int],
    home_lat: float,
    home_lng: float,
    radius_km: float,
    cell_deg: float,
    recent_weeks: int = 4,
) -> list[RegionScore]:
    """Rank grid regions within radius by expected choice-fungi activity in ``months``."""
    # Per (region, taxon): observations in the target months vs. all months.
    rows = con.execute(
        f"""
        WITH tot AS (
            SELECT region_id, center_lat, center_lng, taxon_id, sum(cnt) AS total_cnt
            FROM phenology
            WHERE taxon_id IN ({_in(taxon_ids)})
            GROUP BY region_id, center_lat, center_lng, taxon_id
        ),
        win AS (
            SELECT region_id, taxon_id, sum(cnt) AS month_cnt
            FROM phenology
            WHERE taxon_id IN ({_in(taxon_ids)}) AND month IN ({_in(months)})
            GROUP BY region_id, taxon_id
        )
        SELECT tot.region_id, tot.center_lat, tot.center_lng, tot.taxon_id,
               COALESCE(win.month_cnt, 0) AS month_cnt, tot.total_cnt
        FROM tot LEFT JOIN win USING (region_id, taxon_id)
        WHERE COALESCE(win.month_cnt, 0) > 0
        """,
        [*taxon_ids, *taxon_ids, *months],
    ).fetchall()

    names = dict(con.execute("SELECT taxon_id, common_name FROM taxa").fetchall())
    recent = _recent_counts(con, cell_deg, taxon_ids, recent_weeks)

    # Group per region, applying the distance filter and the score formula.
    regions: dict[str, dict[str, Any]] = {}
    for region_id, clat, clng, taxon_id, month_cnt, total_cnt in rows:
        dist = haversine_km(home_lat, home_lng, clat, clng)
        if dist > radius_km:
            continue
        w_pheno = month_cnt / total_cnt if total_cnt else 0.0
        agg = regions.setdefault(
            region_id,
            {"clat": clat, "clng": clng, "dist": dist, "score": 0.0, "species": []},
        )
        agg["score"] += w_pheno * math.log1p(month_cnt)
        agg["species"].append(
            SpeciesHit(taxon_id, names.get(taxon_id, str(taxon_id)), month_cnt, total_cnt, w_pheno)
        )

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
            )
        )

    top_score = max((region.score for region in results), default=0.0)
    for region in results:
        region.score_norm = round(region.score / top_score, 4) if top_score else 0.0
    results.sort(key=lambda region: region.score, reverse=True)
    return results


@dataclass
class CampSite:
    id: str
    name: str
    kind: str
    fee: str | None
    free: bool | None
    center_lat: float
    center_lng: float
    distance_km: float
    source: str
    url: str


def camps_near(
    con: duckdb.DuckDBPyConnection,
    *,
    lat: float,
    lng: float,
    radius_km: float,
    free_only: bool = False,
) -> list[CampSite]:
    """Campsites within ``radius_km`` of a point, ranked free-first then by distance.

    ``free`` is only TRUE where the source explicitly said so; ``free_only`` therefore
    keeps just those (it never guesses that an unpriced site is free). Missing table
    (no camps ingested yet) yields an empty list, mirroring the other modes.
    """
    try:
        rows = con.execute(
            "SELECT id, name, kind, fee, free, lat, lng, source, url FROM campsites"
        ).fetchall()
    except duckdb.CatalogException:
        return []

    # Keep the unrounded distance alongside each site so ranking is exact; distance_km is
    # only rounded for display and must not be the sort key (near-equal sites would tie).
    scored: list[tuple[bool, float, CampSite]] = []
    for site_id, name, kind, fee, free, site_lat, site_lng, source, url in rows:
        if free_only and not free:
            continue
        dist = haversine_km(lat, lng, site_lat, site_lng)
        if dist > radius_km:
            continue
        site = CampSite(
            id=site_id,
            name=name,
            kind=kind,
            fee=fee,
            free=free,
            center_lat=site_lat,
            center_lng=site_lng,
            distance_km=round(dist, 1),
            source=source,
            url=url,
        )
        scored.append((free is not True, dist, site))
    # Free sites first (True > None/False), then nearest by true distance.
    scored.sort(key=lambda item: (item[0], item[1]))
    return [site for _, _, site in scored]


def place_calendar(
    con: duckdb.DuckDBPyConnection, *, region_id: str, taxon_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """12-month activity for a region: total count + per-species breakdown per month."""
    rows = con.execute(
        f"""
        SELECT month, taxon_id, cnt FROM phenology
        WHERE region_id = ? AND taxon_id IN ({_in(taxon_ids)})
        """,
        [region_id, *taxon_ids],
    ).fetchall()
    names = dict(con.execute("SELECT taxon_id, common_name FROM taxa").fetchall())
    calendar: dict[int, dict[str, Any]] = {
        month: {"total": 0, "species": {}} for month in range(1, 13)
    }
    for month, taxon_id, cnt in rows:
        bucket = calendar[month]
        bucket["total"] += cnt
        bucket["species"][names.get(taxon_id, str(taxon_id))] = cnt
    return calendar


def alerts(
    con: duckdb.DuckDBPyConnection,
    *,
    taxon_ids: list[int],
    home_lat: float,
    home_lng: float,
    radius_km: float,
    cell_deg: float,
    weeks: int = 4,
) -> list[dict[str, Any]]:
    """Regions with fresh (trailing ``weeks``) observations of target species — 'fruiting now'."""
    cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    binned = _BINNED.format(cell=cell_deg)
    rows = con.execute(
        f"""
        SELECT region_id, center_lat, center_lng, taxon_id, count(*) AS cnt,
               max(observed_on) AS last_seen
        FROM ({binned})
        WHERE observed_on >= ? AND taxon_id IN ({_in(taxon_ids)})
        GROUP BY region_id, center_lat, center_lng, taxon_id
        """,
        [cutoff, *taxon_ids],
    ).fetchall()
    names = dict(con.execute("SELECT taxon_id, common_name FROM taxa").fetchall())

    by_region: dict[str, dict[str, Any]] = {}
    for region_id, clat, clng, taxon_id, cnt, last_seen in rows:
        dist = haversine_km(home_lat, home_lng, clat, clng)
        if dist > radius_km:
            continue
        entry = by_region.setdefault(
            region_id,
            {
                "region_id": region_id,
                "center_lat": clat,
                "center_lng": clng,
                "distance_km": round(dist, 1),
                "total": 0,
                "species": [],
            },
        )
        entry["total"] += cnt
        entry["species"].append(
            {
                "taxon_id": taxon_id,
                "common_name": names.get(taxon_id, str(taxon_id)),
                "count": cnt,
                "last_seen": str(last_seen),
            }
        )
    results = list(by_region.values())
    results.sort(key=lambda region: region["total"], reverse=True)
    return results
