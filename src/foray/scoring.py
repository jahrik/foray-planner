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
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, LiteralString, cast

import psycopg

# The "research-grade only" invariant (see AGENTS.md) is enforced here, centrally, rather
# than trusted from the iNat API query param it started as (inat.py) - any row that lands in
# `observations` some other way (a future bulk loader, a manual insert) still can't leak into
# scoring.
_BINNED = """
SELECT
    o.*,
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
_CENTER_LAT = "COALESCE(AVG(lat) FILTER (WHERE NOT COALESCE(obscured, false)), AVG(lat))::double precision"
_CENTER_LNG = "COALESCE(AVG(lng) FILTER (WHERE NOT COALESCE(obscured, false)), AVG(lng))::double precision"


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
                       {_CENTER_LAT} AS center_lat,
                       {_CENTER_LNG} AS center_lng,
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
                       {_CENTER_LAT} AS center_lat,
                       {_CENTER_LNG} AS center_lng,
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
        # Fresh tables have no planner statistics until autovacuum gets to them - without this,
        # requests right after a refresh could still get seq-scan plans despite the indexes above.
        con.execute("ANALYZE phenology")
        con.execute("ANALYZE regions")


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    earth_radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    inner = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return 2 * earth_radius_km * math.asin(math.sqrt(inner))


@dataclass
class SpeciesHit:
    taxon_id: int
    name: str
    common_name: str | None
    month_count: int
    total_count: int
    w_pheno: float


def _genus_name_map(con: psycopg.Connection, taxon_ids: Collection[int]) -> dict[int, tuple[str, str | None]]:
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


def _recent_counts(con: psycopg.Connection, cell_deg: float, taxon_ids: list[int], weeks: int) -> dict[str, int]:
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
            WHERE observed_on >= %s AND {_taxon_filter(taxon_ids)}
            GROUP BY region_id
            """,
        ),
        [cutoff, *taxon_ids],
    ).fetchall()
    return dict(rows)


def _in(ids: list[int]) -> str:
    """SQL fragment for ``IN (...)``. An empty list becomes the literal ``NULL`` (matches
    nothing, valid SQL) rather than an empty ``IN ()``, which Postgres rejects as a syntax
    error - e.g. when ``months`` is empty.
    """
    return ",".join("%s" for _ in ids) if ids else "NULL"


def _taxon_filter(taxon_ids: list[int], column: str = "taxon_id") -> str:
    """SQL condition for a ``taxon_id`` restriction. An empty list means "no genera selected"
    (issue #79 Phase 2) - unlike ``_in()``, that must mean "no filter" (``TRUE``, match every
    taxon), not "match nothing", so a fresh device with no selection sees everything nearby
    instead of an empty result.
    """
    return f"{column} IN ({_in(taxon_ids)})" if taxon_ids else "TRUE"


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
                WHERE {_taxon_filter(taxon_ids)}
                GROUP BY region_id, taxon_id
            ),
            win AS (
                SELECT region_id, taxon_id, sum(cnt)::bigint AS month_cnt
                FROM phenology
                WHERE {_taxon_filter(taxon_ids)} AND month IN ({_in(months)})
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

    genera = _genus_name_map(con, {row[3] for row in rows})
    recent = _recent_counts(con, cell_deg, taxon_ids, recent_weeks)

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


def _project_to_plane(ref_lat: float, ref_lng: float, lat: float, lng: float) -> tuple[float, float]:
    """Local tangent-plane projection (x=east km, y=north km) centered on ``ref_lat``/``ref_lng``.

    Same flat-degree approximation ``camps_near``/``trails_near`` already use for bbox
    prefilters (``km / 111.0``), just applied once per point instead of per-bbox-edge. Fine
    at corridor scale (tens to low hundreds of km); ``ref_lat`` is fixed for every point in a
    given call so the longitude scale distortion is consistent, not per-point re-biased.
    """
    y = (lat - ref_lat) * 111.0
    x = (lng - ref_lng) * 111.0 * max(abs(math.cos(math.radians(ref_lat))), 0.01)
    return x, y


def _segment_progress_and_offset(px: float, py: float, dx: float, dy: float) -> tuple[float, float]:
    """Project point ``(px, py)`` onto the segment from the origin to ``(dx, dy)``.

    Returns ``(t_clamped, offset_km)``: ``t_clamped`` is the projection parameter clamped to
    ``[0, 1]`` (so a point beyond either end measures its offset to that endpoint, not the
    infinite line - the correct corridor semantics), and ``offset_km`` is the perpendicular
    distance from the point to the clamped projection, i.e. the corridor-width test.
    """
    seg_len2 = dx * dx + dy * dy
    if seg_len2 == 0:
        return 0.0, math.hypot(px, py)
    t = (px * dx + py * dy) / seg_len2
    t_clamped = max(0.0, min(1.0, t))
    proj_x, proj_y = t_clamped * dx, t_clamped * dy
    offset_km = math.hypot(px - proj_x, py - proj_y)
    return t_clamped, offset_km


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
    dx, dy = _project_to_plane(start_lat, start_lng, dest_lat, dest_lng)
    total_km = haversine_km(start_lat, start_lng, dest_lat, dest_lng)

    def keep(clat: float, clng: float) -> tuple[bool, float]:
        px, py = _project_to_plane(start_lat, start_lng, clat, clng)
        t, offset_km = _segment_progress_and_offset(px, py, dx, dy)
        # _segment_progress_and_offset's degenerate-segment branch always returns t=0.0, which
        # would make every candidate's progress 0 (collapsing the sort order) - use the radial
        # offset (= distance from start in that branch) as progress instead, matching the
        # docstring's fallback.
        progress_km = offset_km if total_km == 0 else t * total_km
        return offset_km <= corridor_km, progress_km

    return _rank_candidates(
        con, months=months, taxon_ids=taxon_ids, cell_deg=cell_deg, recent_weeks=recent_weeks, keep=keep
    )


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


def land_near(con: psycopg.Connection, *, lat: float, lng: float, radius_km: float) -> list[LandUnit]:
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
    con: psycopg.Connection, *, lat: float, lng: float, radius_km: float, with_camp_distance: bool = True
) -> list[Trail]:
    """Trails whose representative point is within ``radius_km`` of a hotspot, nearest first.

    A cheap bbox-vs-envelope prefilter in SQL (the stored geometry needs no spatial types)
    narrows candidates; the exact cut and ordering use ``haversine_km`` on each trail's stored
    center. Each trail is annotated with the distance to the nearest cached campsite so the UI can
    show the "park → hike → fungi" chain - unless ``with_camp_distance`` is False, which skips that
    O(trails-in-bbox * all-campsites) scan (measured ~13s against a 1M-row/17k-camp production
    cache). ``plan_route`` sets it False: each stop already carries its own selected camp, so a
    second "nearest camp to this trail" figure would be redundant there, and it calls this per stop
    (up to ``max_stops`` times) where the full-catalog scan's cost multiplies fast. No rows
    ingested yet yields an empty list, mirroring ``camps_near`` / ``land_near``.
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
    camps = con.execute("SELECT lat, lng FROM campsites").fetchall() if with_camp_distance else []

    scored: list[tuple[float, Trail]] = []
    for trail_id, name, kind, source, url, clat, clng, geojson in rows:
        dist = haversine_km(lat, lng, clat, clng)
        if dist > radius_km:
            continue
        camp_dist = (
            min(
                (haversine_km(clat, clng, camp_lat, camp_lng) for camp_lat, camp_lng in camps),
                default=None,
            )
            if with_camp_distance
            else None
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


_CALENDAR_SPECIES_PER_MONTH = 15


def place_calendar(con: psycopg.Connection, *, region_id: str, taxon_ids: list[int]) -> dict[int, dict[str, Any]]:
    """12-month activity for a region: total count + per-species breakdown per month.

    ``total`` always reflects every matching row, but the breakdown itself is capped to the
    top ``_CALENDAR_SPECIES_PER_MONTH`` taxa per month - with an empty ``taxon_ids`` filter
    (issue #79: "no genus selected" means every catalog genus, ~6,018 of them), an uncapped
    breakdown would both bloat the response and key `dict[str, int]` by display name, where
    two genera sharing the same label would silently overwrite each other.
    """
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            SELECT month, taxon_id, cnt FROM phenology
            WHERE region_id = %s AND {_taxon_filter(taxon_ids)}
            """,
        ),
        [region_id, *taxon_ids],
    ).fetchall()
    genera = _genus_name_map(con, {row[1] for row in rows})
    calendar: dict[int, dict[str, Any]] = {month: {"total": 0, "species": {}} for month in range(1, 13)}
    per_month_counts: dict[int, dict[int, int]] = {month: {} for month in range(1, 13)}
    for month, taxon_id, cnt in rows:
        calendar[month]["total"] += cnt
        per_month_counts[month][taxon_id] = cnt

    for month, counts in per_month_counts.items():
        top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:_CALENDAR_SPECIES_PER_MONTH]
        species: dict[str, int] = {}
        for taxon_id, cnt in top:
            name, common_name = genera.get(taxon_id, (str(taxon_id), None))
            label = f"{name} ({common_name})" if common_name else name
            if label in species:
                label = f"{label} #{taxon_id}"  # disambiguate a display-name collision
            species[label] = cnt
        calendar[month]["species"] = species
    return calendar


def recent_observations(
    con: psycopg.Connection,
    *,
    region_id: str,
    taxon_ids: list[int],
    cell_deg: float,
    months: list[int],
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Most recent observations in a region, newest first - the source list for photo thumbnails."""
    binned = _BINNED.format(cell=cell_deg)
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            SELECT o.id, o.taxon_id, o.observed_on, o.place_guess, o.uri, o.obscured
            FROM ({binned}) o
            WHERE o.region_id = %s AND {_taxon_filter(taxon_ids, "o.taxon_id")} AND o.month IN ({_in(months)})
            ORDER BY o.observed_on DESC
            LIMIT %s
            """,
        ),
        [region_id, *taxon_ids, *months, limit],
    ).fetchall()
    genera = _genus_name_map(con, {row[1] for row in rows})
    results = []
    for obs_id, taxon_id, observed_on, place_guess, uri, obscured in rows:
        name, common_name = genera.get(taxon_id, (str(taxon_id), None))
        results.append(
            {
                "id": obs_id,
                "taxon_id": taxon_id,
                "name": name,
                "common_name": common_name,
                "observed_on": observed_on.isoformat() if observed_on else None,
                "place_guess": place_guess,
                "uri": uri,
                "obscured": bool(obscured),
            }
        )
    return results


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
    # Centers computed once per region_id across every matching taxon (not per region+taxon
    # below) - a region with several target species shouldn't get a decoy-shifted center just
    # because one of those species' rows here happen to be entirely obscured while another's
    # aren't (Copilot review, PR #184).
    region_centers = {
        region_id: (clat, clng)
        for region_id, clat, clng in con.execute(
            cast(
                LiteralString,
                f"""
                SELECT region_id, {_CENTER_LAT} AS center_lat, {_CENTER_LNG} AS center_lng
                FROM ({binned})
                WHERE observed_on >= %s AND {_taxon_filter(taxon_ids)}
                GROUP BY region_id
                """,
            ),
            [cutoff, *taxon_ids],
        ).fetchall()
    }
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            SELECT region_id,
                   taxon_id, count(*) AS cnt,
                   max(observed_on) AS last_seen,
                   (array_agg(place_guess ORDER BY observed_on DESC))[1] AS place_guess,
                   (array_agg(uri ORDER BY observed_on DESC))[1] AS uri,
                   (array_agg(obscured ORDER BY observed_on DESC))[1] AS obscured
            FROM ({binned})
            WHERE observed_on >= %s AND {_taxon_filter(taxon_ids)}
            GROUP BY region_id, taxon_id
            """,
        ),
        [cutoff, *taxon_ids],
    ).fetchall()
    genera = _genus_name_map(con, {row[1] for row in rows})

    by_region: dict[str, dict[str, Any]] = {}
    for region_id, taxon_id, cnt, last_seen, place_guess, uri, obscured in rows:
        clat, clng = region_centers[region_id]
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
        name, common_name = genera.get(taxon_id, (str(taxon_id), None))
        entry["species"].append(
            {
                "taxon_id": taxon_id,
                "name": name,
                "common_name": common_name,
                "count": cnt,
                "last_seen": str(last_seen),
                "place_guess": place_guess,
                "uri": uri,
                "obscured": obscured or False,
            }
        )
    results = list(by_region.values())
    results.sort(key=lambda region: region["total"], reverse=True)
    return results


def precise_observations(
    con: psycopg.Connection,
    *,
    taxon_ids: list[int],
    home_lat: float,
    home_lng: float,
    radius_km: float,
    months: list[int],
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Individually-plottable observations within ``radius_km`` whose cached coordinate is
    known-precise (``obscured = false``, i.e. live-verified against iNat, not a randomized
    geoprivacy decoy - see ``ingest.resync``). Everything ``NULL``/``true`` stays out of this
    path entirely and is only ever shown via the coarse region circle (issue #161) - this
    query never widens what a caller already sees, since ``obscured = false`` is exactly the
    subset iNat itself already publishes as an exact point.

    Same bbox-prefilter-then-haversine-cut technique as ``camps_near``/``trails_near``. A
    densely-observed area can easily clear ``limit`` before the radius cut even applies (a
    real dev-DB check against Seattle found ~7,800 candidates in-radius for one month) - the
    frontend has no marker-clustering yet (issue #161's "why not now"), so dumping every match
    unclustered would bury the map. ``ORDER BY observed_on DESC`` caps this to the freshest
    sightings first, which also matches the "is this still active" value of an individual pin
    better than an arbitrary cutoff would.
    """
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(abs(math.cos(math.radians(home_lat))), 0.01))
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            SELECT o.id, o.taxon_id, o.lat, o.lng, o.observed_on, o.uri
            FROM observations o
            WHERE o.quality_grade = 'research' AND o.obscured = FALSE
              AND o.lat BETWEEN %s AND %s AND o.lng BETWEEN %s AND %s
              AND {_taxon_filter(taxon_ids)} AND o.month IN ({_in(months)})
            ORDER BY o.observed_on DESC
            LIMIT %s
            """,
        ),
        [home_lat - dlat, home_lat + dlat, home_lng - dlng, home_lng + dlng, *taxon_ids, *months, limit],
    ).fetchall()
    genera = _genus_name_map(con, {row[1] for row in rows})

    results = []
    for obs_id, taxon_id, lat, lng, observed_on, uri in rows:
        dist = haversine_km(home_lat, home_lng, lat, lng)
        if dist > radius_km:
            continue
        name, common_name = genera.get(taxon_id, (str(taxon_id), None))
        results.append(
            {
                "id": obs_id,
                "taxon_id": taxon_id,
                "name": name,
                "common_name": common_name,
                "lat": lat,
                "lng": lng,
                "observed_on": observed_on.isoformat() if observed_on else None,
                "uri": uri,
            }
        )
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
    progress_km: float  # distance along the start->destination chord (0 at start)
    drive_km_from_prev: float  # great-circle leg from the previous stop (or start for stop 1)
    cumulative_drive_km: float  # running total from start, actual point-to-point (not progress_km)
    camp: CampSite | None  # closest free camp (or closest of any kind if none is free-tagged)
    camp_is_free: bool
    trail: Trail | None  # closest trail in range, if any
    trail_distance_km: float | None


@dataclass
class TripPlan:
    start_lat: float
    start_lng: float
    destination_lat: float
    destination_lng: float
    destination_name: str | None  # region_id when auto-picked; None for a caller-supplied destination
    auto_destination: bool
    corridor_km: float
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
    cell_deg: float,
    start_lat: float,
    start_lng: float,
    destination_lat: float | None = None,
    destination_lng: float | None = None,
    corridor_km: float = 60.0,
    auto_pick_radius_km: float = 300.0,
    auto_pick_min_km: float = 50.0,
    recent_weeks: int = 4,
    max_stops: int = 5,
    max_drive_km: float = 400.0,
    camp_radius_km: float = 40.0,
    require_free_camp: bool = False,
    min_score_norm: float = 0.0,
) -> TripPlan:
    """Plan a trip from ``start`` to ``destination`` (auto-picked if not given), stopping at the
    best fruiting spots - each with a nearby camp and trail - along the way.

    Two phases:

    1. **Pick a destination**, if the caller didn't supply one: run ``rank_destinations``
       radially from ``start`` within ``auto_pick_radius_km``, drop anything closer than
       ``auto_pick_min_km`` (don't auto-pick something next door - this is meant to be a
       trip), and take the best-scoring survivor. Nothing clearing that bar yields an empty
       plan rather than silently falling back to the old radial-only behaviour.
    2. **Select + order stops along the corridor**: ``rank_destinations_corridor`` (already
       score-desc, with ``distance_km`` repurposed as progress-along-line) - annotate each
       with its nearest campsite (``camps_near``, free-first) and nearest trail
       (``trails_near``), drop regions below ``min_score_norm`` or - when
       ``require_free_camp`` - without a free camp inside ``camp_radius_km``, keep the top
       ``max_stops`` by score, then sort by progress along the line ("along the way" order,
       not nearest-neighbour). Walking that order, any stop whose leg from the current position
       exceeds ``max_drive_km`` is skipped individually (counted in ``skipped_unreachable``)
       rather than assumed to make every later stop unreachable too - legs aren't guaranteed
       monotonic under progress ordering (see below). Great-circle distance stands in for real
       drive time until road routing lands (a documented follow-up).

    Straight-line v1: the "corridor" is a buffer around the great-circle chord, not a real
    road route, so a wide corridor with off-axis stops can occasionally zigzag rather than
    monotonically recede from the current position - this is exactly why unreachable stops are
    skipped individually rather than truncating the rest of the itinerary.

    Missing tables (nothing ingested yet) surface as ``rank_destinations``/
    ``rank_destinations_corridor`` raising, mirroring the other modes; an empty candidate set
    yields an empty plan.
    """
    auto = destination_lat is None or destination_lng is None
    destination_name: str | None = None
    if auto:
        picks = rank_destinations(
            con,
            months=months,
            taxon_ids=taxon_ids,
            home_lat=start_lat,
            home_lng=start_lng,
            radius_km=auto_pick_radius_km,
            cell_deg=cell_deg,
            recent_weeks=recent_weeks,
        )
        picks = [
            pick
            for pick in picks
            if haversine_km(start_lat, start_lng, pick.center_lat, pick.center_lng) >= auto_pick_min_km
        ]
        if not picks:
            return TripPlan(
                start_lat=start_lat,
                start_lng=start_lng,
                destination_lat=start_lat,
                destination_lng=start_lng,
                destination_name=None,
                auto_destination=True,
                corridor_km=corridor_km,
                months=months,
                n_stops=0,
                total_drive_km=0.0,
                stops=[],
                skipped_unreachable=0,
            )
        destination_lat, destination_lng = picks[0].center_lat, picks[0].center_lng
        destination_name = picks[0].region_id

    ranked = rank_destinations_corridor(
        con,
        months=months,
        taxon_ids=taxon_ids,
        start_lat=start_lat,
        start_lng=start_lng,
        dest_lat=destination_lat,
        dest_lng=destination_lng,
        corridor_km=corridor_km,
        cell_deg=cell_deg,
        recent_weeks=recent_weeks,
    )

    # Select - annotate + filter, preserving the score-desc order rank_destinations_corridor returns.
    candidates: list[tuple[RegionScore, CampSite | None, bool, Trail | None]] = []
    for region in ranked:
        if region.score_norm < min_score_norm:
            continue
        # camps_near ranks free-first, so its nearest result is the nearest *free* camp when one
        # is in range, else the nearest of any kind - one query answers both cases.
        nearby_camps = camps_near(con, lat=region.center_lat, lng=region.center_lng, radius_km=camp_radius_km)
        camp = nearby_camps[0] if nearby_camps else None
        camp_is_free = camp is not None and camp.free is True
        if require_free_camp and not camp_is_free:
            continue
        nearby_trails = trails_near(
            con, lat=region.center_lat, lng=region.center_lng, radius_km=camp_radius_km, with_camp_distance=False
        )
        trail = nearby_trails[0] if nearby_trails else None
        candidates.append((region, camp, camp_is_free, trail))
        if len(candidates) >= max_stops:
            break

    # Order - by progress along the start->destination line ("along the way"), not nearest-neighbour.
    candidates.sort(key=lambda item: item[0].distance_km)

    cur_lat, cur_lng = start_lat, start_lng
    stops: list[Stop] = []
    cumulative = 0.0
    skipped = 0
    for region, camp, camp_is_free, trail in candidates:
        leg = haversine_km(cur_lat, cur_lng, region.center_lat, region.center_lng)
        if leg > max_drive_km:
            # Progress-ordered, not nearest-neighbour, so legs aren't guaranteed monotonic (a wide
            # corridor can zigzag off-axis) - skip just this stop rather than assuming everything
            # still to come is unreachable too.
            skipped += 1
            continue
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
                progress_km=region.distance_km,
                drive_km_from_prev=round(leg, 1),
                cumulative_drive_km=round(cumulative, 1),
                camp=camp,
                camp_is_free=camp_is_free,
                trail=trail,
                trail_distance_km=trail.distance_km if trail else None,
            )
        )
        cur_lat, cur_lng = region.center_lat, region.center_lng

    return TripPlan(
        start_lat=start_lat,
        start_lng=start_lng,
        destination_lat=destination_lat,
        destination_lng=destination_lng,
        destination_name=destination_name,
        auto_destination=auto,
        corridor_km=corridor_km,
        months=months,
        n_stops=len(stops),
        total_drive_km=round(cumulative, 1),
        stops=stops,
        skipped_unreachable=skipped,
    )
