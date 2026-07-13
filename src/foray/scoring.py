"""Phenology materialization + the three scoring modes.

Regions are uniform lat/lng grid cells (``cell_deg`` wide). All scoring is built from
three primitives per (taxon, region, month):

* **w_pheno** - share of that taxon's regional observations that fall in the target
  month(s): "is it in season here?" (0..1)
* **abundance** - log-scaled observation count: "how reliably does it show up here?"
* **recency** - this-year observations in a trailing window: "is it going off now?"

Fix the month axis -> rank regions (destinations). Fix the region -> rank months
(calendar). Fix species + recency -> alerts.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass
from typing import Any, LiteralString, cast

import psycopg

_BINNED = """
SELECT
    o.*,
    CAST(floor(o.lat / {cell}) AS INTEGER) AS ilat,
    CAST(floor(o.lng / {cell}) AS INTEGER) AS ilng,
    (CAST(floor(o.lat / {cell}) AS INTEGER))::text || '_' ||
        (CAST(floor(o.lng / {cell}) AS INTEGER))::text AS region_id
FROM observations o
"""


def build_phenology(con: psycopg.Connection, cell_deg: float) -> None:
    """(Re)materialize the ``regions`` and ``phenology`` tables from ``observations``.

    Wrapped in one transaction (the connection otherwise runs autocommit) so a concurrent
    reader never sees a mid-rebuild state where the tables are dropped but not yet
    recreated.
    """
    binned = _BINNED.format(cell=cell_deg)
    with con.transaction():
        con.execute("DROP TABLE IF EXISTS phenology")
        con.execute("DROP TABLE IF EXISTS regions")
        con.execute(
            cast(
                LiteralString,
                f"""
                CREATE TABLE phenology AS
                SELECT region_id,
                       AVG(lat)::double precision AS center_lat,
                       AVG(lng)::double precision AS center_lng,
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
                       AVG(lat)::double precision AS center_lat,
                       AVG(lng)::double precision AS center_lng,
                       count(*) AS n_obs,
                       count(DISTINCT taxon_id) AS n_taxa
                FROM ({binned})
                GROUP BY region_id
                """,
            )
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
    con: psycopg.Connection, cell_deg: float, taxon_ids: list[int], weeks: int
) -> dict[str, int]:
    cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    binned = _BINNED.format(cell=cell_deg)
    # cast: the query is built from a fixed template + `_in()`'s placeholder-count text
    # (never user data), but psycopg's LiteralString typing can't verify that statically.
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            SELECT region_id, count(*) AS cnt
            FROM ({binned})
            WHERE observed_on >= %s AND taxon_id IN ({_in(taxon_ids)})
            GROUP BY region_id
            """,
        ),
        [cutoff, *taxon_ids],
    ).fetchall()
    return dict(rows)


def _in(ids: list[int]) -> str:
    """SQL fragment for ``IN (...)``. An empty list becomes the literal ``NULL`` (matches
    nothing, valid SQL) rather than an empty ``IN ()``, which Postgres rejects as a syntax
    error - e.g. when ``Config.species`` is empty and ``taxon_ids`` comes back ``[]``.
    """
    return ",".join("%s" for _ in ids) if ids else "NULL"


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
    """Rank grid regions within radius by expected choice-fungi activity in ``months``."""
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
                WHERE taxon_id IN ({_in(taxon_ids)})
                GROUP BY region_id, taxon_id
            ),
            win AS (
                SELECT region_id, taxon_id, sum(cnt)::bigint AS month_cnt
                FROM phenology
                WHERE taxon_id IN ({_in(taxon_ids)}) AND month IN ({_in(months)})
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
    con: psycopg.Connection,
    *,
    lat: float,
    lng: float,
    radius_km: float,
    free_only: bool = False,
) -> list[CampSite]:
    """Campsites within ``radius_km`` of a point, ranked free-first then by distance.

    ``free`` is only TRUE where the source explicitly said so; ``free_only`` therefore
    keeps just those (it never guesses that an unpriced site is free). A cheap bbox
    prefilter in SQL (same technique as ``land_near``/``trails_near``) narrows candidates
    before the exact ``haversine_km`` cut in Python - `campsites` has no bbox columns of its
    own (it's points, not polygons), so the filter is directly against `lat`/`lng`. No rows
    ingested yet yields an empty list, mirroring the other modes.
    """
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(abs(math.cos(math.radians(lat))), 0.01))
    rows = con.execute(
        """
        SELECT id, name, kind, fee, free, lat, lng, source, url FROM campsites
        WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
        """,
        [lat - dlat, lat + dlat, lng - dlng, lng + dlng],
    ).fetchall()

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


@dataclass
class LandUnit:
    id: str
    agency: str
    unit: str
    source: str
    url: str
    geometry: dict[str, Any]  # parsed GeoJSON geometry, ready for Leaflet


def land_near(
    con: psycopg.Connection, *, lat: float, lng: float, radius_km: float
) -> list[LandUnit]:
    """Public-land ownership polygons whose bounding box overlaps the home disk.

    Filtering is a cheap bbox-vs-envelope overlap in SQL (the stored geometry needs no spatial
    types); it's coarse on purpose - the map just shades approximate ownership. No rows
    ingested yet yields an empty list, mirroring ``camps_near``.
    """
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(abs(math.cos(math.radians(lat))), 0.01))
    rows = con.execute(
        """
        SELECT id, agency, unit, source, url, geojson FROM public_land
        WHERE min_lat <= %s AND max_lat >= %s AND min_lng <= %s AND max_lng >= %s
        """,
        [lat + dlat, lat - dlat, lng + dlng, lng - dlng],
    ).fetchall()
    return [
        LandUnit(
            id=land_id,
            agency=agency,
            unit=unit,
            source=source,
            url=url,
            geometry=json.loads(geojson),
        )
        for land_id, agency, unit, source, url, geojson in rows
    ]


@dataclass
class Trail:
    id: str
    name: str
    kind: str
    source: str
    url: str
    center_lat: float
    center_lng: float
    distance_km: float  # from the hotspot to the trail's representative point
    camp_distance_km: float | None  # nearest cached campsite to the trail ("park → hike → fungi")
    geometry: dict[str, Any]  # parsed GeoJSON geometry, ready for Leaflet


def trails_near(
    con: psycopg.Connection, *, lat: float, lng: float, radius_km: float
) -> list[Trail]:
    """Trails whose representative point is within ``radius_km`` of a hotspot, nearest first.

    A cheap bbox-vs-envelope prefilter in SQL (the stored geometry needs no spatial types)
    narrows candidates; the exact cut and ordering use ``haversine_km`` on each trail's stored
    center. Each trail is annotated with the distance to the nearest cached campsite so the UI can
    show the "park → hike → fungi" chain. No rows ingested yet yields an empty list, mirroring
    ``camps_near`` / ``land_near``.
    """
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(abs(math.cos(math.radians(lat))), 0.01))
    rows = con.execute(
        """
        SELECT id, name, kind, source, url, center_lat, center_lng, geojson FROM trails
        WHERE min_lat <= %s AND max_lat >= %s AND min_lng <= %s AND max_lng >= %s
        """,
        [lat + dlat, lat - dlat, lng + dlng, lng - dlng],
    ).fetchall()
    # Nearest-campsite distance is a per-trail annotation; fetch the camp points once and reuse.
    camps = con.execute("SELECT lat, lng FROM campsites").fetchall()

    scored: list[tuple[float, Trail]] = []
    for trail_id, name, kind, source, url, clat, clng, geojson in rows:
        dist = haversine_km(lat, lng, clat, clng)
        if dist > radius_km:
            continue
        camp_dist = min(
            (haversine_km(clat, clng, camp_lat, camp_lng) for camp_lat, camp_lng in camps),
            default=None,
        )
        scored.append(
            (
                dist,
                Trail(
                    id=trail_id,
                    name=name,
                    kind=kind,
                    source=source,
                    url=url,
                    center_lat=clat,
                    center_lng=clng,
                    distance_km=round(dist, 1),
                    camp_distance_km=round(camp_dist, 1) if camp_dist is not None else None,
                    geometry=json.loads(geojson),
                ),
            )
        )
    # Rank on the unrounded distance so near-ties keep their true order (matches ``camps_near``);
    # the rounded ``distance_km`` is display-only.
    scored.sort(key=lambda item: item[0])
    return [trail for _, trail in scored]


def place_calendar(
    con: psycopg.Connection, *, region_id: str, taxon_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """12-month activity for a region: total count + per-species breakdown per month."""
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            SELECT month, taxon_id, cnt FROM phenology
            WHERE region_id = %s AND taxon_id IN ({_in(taxon_ids)})
            """,
        ),
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
    con: psycopg.Connection,
    *,
    taxon_ids: list[int],
    home_lat: float,
    home_lng: float,
    radius_km: float,
    cell_deg: float,
    weeks: int = 4,
) -> list[dict[str, Any]]:
    """Regions with fresh (trailing ``weeks``) observations of target species - 'fruiting now'."""
    cutoff = (dt.date.today() - dt.timedelta(weeks=weeks)).isoformat()
    binned = _BINNED.format(cell=cell_deg)
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            SELECT region_id,
                   AVG(lat)::double precision AS center_lat,
                   AVG(lng)::double precision AS center_lng,
                   taxon_id, count(*) AS cnt,
                   max(observed_on) AS last_seen
            FROM ({binned})
            WHERE observed_on >= %s AND taxon_id IN ({_in(taxon_ids)})
            GROUP BY region_id, taxon_id
            """,
        ),
        [cutoff, *taxon_ids],
    ).fetchall()
    names = dict(con.execute("SELECT taxon_id, common_name FROM taxa").fetchall())

    # Fetch the most recent observation per (region, taxon) for place_guess + uri.
    recent_obs = con.execute(
        cast(
            LiteralString,
            f"""
            SELECT DISTINCT ON (region_id, taxon_id)
                   region_id, taxon_id, place_guess, uri, obscured
            FROM ({binned})
            WHERE observed_on >= %s AND taxon_id IN ({_in(taxon_ids)})
            ORDER BY region_id, taxon_id, observed_on DESC
            """,
        ),
        [cutoff, *taxon_ids],
    ).fetchall()
    obs_detail: dict[tuple[str, int], dict[str, Any]] = {}
    for region_id, taxon_id, place_guess, uri, obscured in recent_obs:
        obs_detail[(region_id, taxon_id)] = {
            "place_guess": place_guess,
            "uri": uri,
            "obscured": obscured or False,
        }

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
        detail = obs_detail.get((region_id, taxon_id), {})
        entry["species"].append(
            {
                "taxon_id": taxon_id,
                "common_name": names.get(taxon_id, str(taxon_id)),
                "count": cnt,
                "last_seen": str(last_seen),
                "place_guess": detail.get("place_guess"),
                "uri": detail.get("uri"),
                "obscured": detail.get("obscured", False),
            }
        )
    results = list(by_region.values())
    results.sort(key=lambda region: region["total"], reverse=True)
    return results


@dataclass
class Stop:
    """One week-long stay in a planned trip: a destination + how you get there + where you sleep."""

    order: int  # 1-based position in the itinerary
    region_id: str
    center_lat: float
    center_lng: float
    score_norm: float  # destination score relative to the best region (0..1)
    n_species: int
    recent_count: int
    species: list[SpeciesHit]
    drive_km_from_prev: float  # great-circle leg from the previous stop (or home for stop 1)
    cumulative_drive_km: float  # running total from home
    camp: CampSite | None  # closest free camp (or closest of any kind if none is free-tagged)
    camp_is_free: bool


@dataclass
class TripPlan:
    home_lat: float
    home_lng: float
    months: list[int]
    n_stops: int
    total_drive_km: float
    stops: list[Stop]
    skipped_unreachable: int  # viable candidates dropped for being past ``max_drive_km`` from route


def plan_route(
    con: psycopg.Connection,
    *,
    months: list[int],
    taxon_ids: list[int],
    home_lat: float,
    home_lng: float,
    radius_km: float,
    cell_deg: float,
    recent_weeks: int = 4,
    max_stops: int = 5,
    max_drive_km: float = 400.0,
    camp_radius_km: float = 40.0,
    require_free_camp: bool = True,
    min_score_norm: float = 0.0,
) -> TripPlan:
    """Sequence the top destinations into a greedy, low-backtrack itinerary of week-long stays.

    Two passes, both built on the existing primitives (no new geography):

    1. **Select** - take ``rank_destinations`` (already score-desc), annotate each with its nearest
       campsite via ``camps_near`` (free-first), drop regions below ``min_score_norm`` or - when
       ``require_free_camp`` - without a free camp inside ``camp_radius_km``, then keep the top
       ``max_stops`` by score. These are the "worth the drive" stops.
    2. **Order** - nearest-neighbour from home: repeatedly hop to the closest remaining stop,
       accumulating ``haversine_km`` legs. Once the closest remaining stop is past ``max_drive_km``
       from the current position everything left is farther still, so the rest are reported as
       ``skipped_unreachable`` rather than forcing an implausible leg. Great-circle distance stands
       in for real drive time until road routing lands (Epic 4 export slice).

    Missing tables (nothing ingested yet) surface as ``rank_destinations`` raising, mirroring the
    other modes; an empty candidate set yields an empty plan.
    """
    ranked = rank_destinations(
        con,
        months=months,
        taxon_ids=taxon_ids,
        home_lat=home_lat,
        home_lng=home_lng,
        radius_km=radius_km,
        cell_deg=cell_deg,
        recent_weeks=recent_weeks,
    )

    # Pass 1 - annotate + filter, preserving the score-desc order rank_destinations returns.
    candidates: list[tuple[RegionScore, CampSite | None, bool]] = []
    for region in ranked:
        if region.score_norm < min_score_norm:
            continue
        # camps_near ranks free-first, so its nearest result is the nearest *free* camp when one
        # is in range, else the nearest of any kind - one query answers both cases.
        nearby = camps_near(
            con, lat=region.center_lat, lng=region.center_lng, radius_km=camp_radius_km
        )
        camp = nearby[0] if nearby else None
        camp_is_free = camp is not None and camp.free is True
        if require_free_camp and not camp_is_free:
            continue
        candidates.append((region, camp, camp_is_free))
        if len(candidates) >= max_stops:
            break

    # Pass 2 - nearest-neighbour ordering from home.
    remaining = candidates[:]
    cur_lat, cur_lng = home_lat, home_lng
    stops: list[Stop] = []
    cumulative = 0.0
    skipped = 0
    while remaining:
        nearest = min(
            range(len(remaining)),
            key=lambda idx: haversine_km(
                cur_lat, cur_lng, remaining[idx][0].center_lat, remaining[idx][0].center_lng
            ),
        )
        region, camp, camp_is_free = remaining[nearest]
        leg = haversine_km(cur_lat, cur_lng, region.center_lat, region.center_lng)
        if leg > max_drive_km:
            skipped = len(remaining)  # closest is unreachable ⇒ so is everything else
            break
        remaining.pop(nearest)
        cumulative += leg
        stops.append(
            Stop(
                order=len(stops) + 1,
                region_id=region.region_id,
                center_lat=region.center_lat,
                center_lng=region.center_lng,
                score_norm=region.score_norm,
                n_species=region.n_species,
                recent_count=region.recent_count,
                species=region.species,
                drive_km_from_prev=round(leg, 1),
                cumulative_drive_km=round(cumulative, 1),
                camp=camp,
                camp_is_free=camp_is_free,
            )
        )
        cur_lat, cur_lng = region.center_lat, region.center_lng

    return TripPlan(
        home_lat=home_lat,
        home_lng=home_lng,
        months=months,
        n_stops=len(stops),
        total_drive_km=round(cumulative, 1),
        stops=stops,
        skipped_unreachable=skipped,
    )
