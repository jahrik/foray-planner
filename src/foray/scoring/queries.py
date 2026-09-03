"""The read repository: point-and-radius lookups over the cached layers.

``camps_near`` / ``land_near`` / ``trails_near`` / ``nearest_trail`` / ``get_trail`` read the
ingested camp / public-land / trail caches; ``place_calendar`` / ``recent_observations`` /
``alerts`` / ``precise_observations`` read ``phenology`` / ``observations``. All of them return
an empty result when nothing is ingested yet, matching each other.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from typing import Any, LiteralString, cast

import psycopg

from foray.cache import region_precip
from foray.geo import haversine_km
from foray.scoring._sql import (
    BINNED,
    CENTER_LAT,
    CENTER_LNG,
    GEOG_POINT,
    genus_name_map,
    sql_in,
    taxon_filter,
)
from foray.scoring.models import CampSite, FireNear, LandUnit, Trail

_CALENDAR_SPECIES_PER_MONTH = 15


def camps_near(
    con: psycopg.Connection,
    *,
    lat: float,
    lng: float,
    radius_km: float,
    free_only: bool = False,
    limit: int | None = None,
) -> list[CampSite]:
    """Campsites within ``radius_km`` of a point, ranked free-first then by distance.

    ``free`` is only TRUE where the source explicitly said so; ``free_only`` therefore
    keeps just those (it never guesses that an unpriced site is free). The radius cut is an
    index-backed ``ST_DWithin`` on the ``geom`` column (issue #268); ``ST_Distance`` returns
    the exact sphere distance in the same query. No rows ingested yet yields an empty list,
    mirroring the other modes. ``limit`` caps the ranked result after sorting, mirroring
    ``trails_near``.
    """
    # Only constant fragments (GEOG_POINT) are interpolated; the annotation keeps that explicit
    # and preserves the module's SQL-injection discipline. Same pattern in the other near-* reads.
    sql: LiteralString = f"""
        WITH pt AS (SELECT {GEOG_POINT} AS g)
        SELECT id, name, kind, fee, free, lat, lng, source, url,
               ST_Distance(c.geom, pt.g) / 1000.0 AS dist_km
        FROM campsites c, pt
        WHERE c.geom IS NOT NULL AND ST_DWithin(c.geom, pt.g, %s)
        """
    rows = con.execute(sql, [lng, lat, radius_km * 1000.0]).fetchall()

    # Keep the unrounded distance alongside each site so ranking is exact; distance_km is
    # only rounded for display and must not be the sort key (near-equal sites would tie).
    scored: list[tuple[bool, float, CampSite]] = []
    for site_id, name, kind, fee, free, site_lat, site_lng, source, url, dist in rows:
        if free_only and not free:
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
    sites = [site for _, _, site in scored]
    return sites[:limit] if limit is not None else sites


def fire_near(
    con: psycopg.Connection,
    *,
    lat: float,
    lng: float,
    radius_km: float,
    status: str | None = None,
    include_geometry: bool = False,
    limit: int | None = None,
) -> list[FireNear]:
    """Active fires and recent burn scars within ``radius_km`` of ``(lat, lng)`` (issue #227).

    Index-backed ``ST_DWithin`` on ``geom``; ``ST_Distance`` gives the exact point-to-perimeter
    distance in the same query (issue #268 - previously a bbox prefilter then a ``haversine_km``
    cut on the representative center). ``status`` filters to ``'active'`` or ``'historical'``
    (both by default). ``include_geometry`` parses the GeoJSON for the map layer; the card /
    scoring paths leave it off. Empty when nothing is ingested yet."""
    try:
        where_status: LiteralString = " AND status = %s" if status else ""
        params: list[Any] = [lng, lat, radius_km * 1000.0]
        if status:
            params.append(status)
        rows = con.execute(
            cast(
                LiteralString,
                f"""
                WITH pt AS (SELECT {GEOG_POINT} AS g)
                SELECT f.id, f.name, f.status, f.fire_year, f.percent_contained, f.gis_acres,
                       f.dominant_severity, f.is_point, f.incident_url, f.center_lat, f.center_lng,
                       f.geojson, ST_Distance(f.geom, pt.g) / 1000.0 AS dist_km
                FROM fire_perimeters f, pt
                WHERE f.geom IS NOT NULL AND ST_DWithin(f.geom, pt.g, %s)
                """
                + where_status,
            ),
            params,
        ).fetchall()
    except psycopg.errors.UndefinedTable:
        con.rollback()
        return []
    scored: list[tuple[float, FireNear]] = []
    for (
        fire_id,
        name,
        fire_status,
        fire_year,
        percent_contained,
        gis_acres,
        dominant_severity,
        is_point,
        incident_url,
        center_lat,
        center_lng,
        geojson,
        dist,
    ) in rows:
        scored.append(
            (
                dist,
                FireNear(
                    id=fire_id,
                    name=name,
                    status=fire_status,
                    fire_year=fire_year,
                    center_lat=center_lat,
                    center_lng=center_lng,
                    distance_km=round(dist, 1),
                    percent_contained=percent_contained,
                    gis_acres=gis_acres,
                    dominant_severity=dominant_severity,
                    is_point=is_point or False,
                    incident_url=incident_url,
                    geometry=json.loads(geojson) if include_geometry and geojson else None,
                ),
            )
        )
    scored.sort(key=lambda item: item[0])
    fires = [fire for _, fire in scored]
    return fires[:limit] if limit is not None else fires


def land_near(con: psycopg.Connection, *, lat: float, lng: float, radius_km: float) -> list[LandUnit]:
    """Public-land ownership polygons within ``radius_km`` of the home point.

    Index-backed ``ST_DWithin`` on ``geom`` (issue #268 - previously a bbox-vs-envelope
    overlap); still coarse on purpose - the map just shades approximate ownership. No rows
    ingested yet yields an empty list, mirroring ``camps_near``.
    """
    sql: LiteralString = f"""
        WITH pt AS (SELECT {GEOG_POINT} AS g)
        SELECT p.id, p.agency, p.unit, p.source, p.url, p.geojson
        FROM public_land p, pt
        WHERE p.geom IS NOT NULL AND ST_DWithin(p.geom, pt.g, %s)
        """
    rows = con.execute(sql, [lng, lat, radius_km * 1000.0]).fetchall()
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


def trails_near(
    con: psycopg.Connection,
    *,
    lat: float,
    lng: float,
    radius_km: float,
    with_camp_distance: bool = True,
    kind: str | None = None,
    limit: int | None = None,
) -> list[Trail]:
    """Trails within ``radius_km`` of a hotspot, nearest first.

    Index-backed ``ST_DWithin`` on ``geom`` for the radius cut; ``ST_Distance`` gives the exact
    point-to-trail distance (issue #268 - previously a bbox prefilter then a ``haversine_km``
    cut on each trail's stored center). Each trail is annotated with the distance to the nearest
    cached campsite so the UI can
    show the "park → hike → fungi" chain - unless ``with_camp_distance`` is False, which skips that
    O(trails-in-bbox * all-campsites) scan (measured ~13s against a 1M-row/17k-camp production
    cache). ``plan_route`` sets it False: each stop already carries its own selected camp, so a
    second "nearest camp to this trail" figure would be redundant there, and it calls this per stop
    (up to ``max_stops`` times) where the full-catalog scan's cost multiplies fast. ``kind``
    restricts to one element class (e.g. ``"trailhead"`` for the destination-card trail list,
    issue #115 follow-up); ``limit`` caps the ranked result after sorting. No rows ingested yet
    yields an empty list, mirroring ``camps_near`` / ``land_near``.
    """
    params: list[Any] = [lng, lat, radius_km * 1000.0]
    kind_filter: LiteralString = ""
    if kind is not None:
        kind_filter = "AND t.kind = %s"
        params.append(kind)
    sql: LiteralString = f"""
        WITH pt AS (SELECT {GEOG_POINT} AS g)
        SELECT t.id, t.name, t.kind, t.source, t.url, t.center_lat, t.center_lng, t.geojson,
               ST_Distance(t.geom, pt.g) / 1000.0 AS dist_km
        FROM trails t, pt
        WHERE t.geom IS NOT NULL AND ST_DWithin(t.geom, pt.g, %s) {kind_filter}
        """
    rows = con.execute(sql, params).fetchall()
    # Nearest-campsite distance is a per-trail annotation; fetch the camp points once and reuse.
    camps = con.execute("SELECT lat, lng FROM campsites").fetchall() if with_camp_distance else []

    scored: list[tuple[float, Trail]] = []
    for trail_id, name, kind, source, url, clat, clng, geojson, dist in rows:
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
    trails = [trail for _, trail in scored]
    return trails[:limit] if limit is not None else trails


def get_trail(con: psycopg.Connection, trail_id: str) -> Trail | None:
    """Single trail row by id, or None if not cached. No camp-distance annotation (see ``trails_near``)."""
    row = con.execute(
        "SELECT id, name, kind, source, url, center_lat, center_lng, geojson FROM trails WHERE id = %s",
        [trail_id],
    ).fetchone()
    if row is None:
        return None
    trail_id_, name, kind, source, url, clat, clng, geojson = row
    return Trail(
        id=trail_id_,
        name=name,
        kind=kind,
        source=source,
        url=url,
        center_lat=clat,
        center_lng=clng,
        distance_km=0.0,
        camp_distance_km=None,
        geometry=json.loads(geojson),
    )


def nearest_trail(con: psycopg.Connection, *, lat: float, lng: float, max_km: float = 2.0) -> Trail | None:
    """Nearest cached path/route to (``lat``, ``lng``), or None if nothing is within ``max_km``.

    Fallback for ``trails.resolve_trail_network`` (issue: "draw the real trail on trailhead
    selection") when OSM has no topological link between a trailhead node and any way/relation -
    a heuristic, not an authoritative link, so callers should label it as such. ``ST_DWithin`` +
    ``geom <-> pt`` KNN sort give true point-to-line distance off the GIST index (issue #268 -
    previously a point-to-vertex haversine over the thinned geometry).
    """
    sql: LiteralString = f"""
        WITH pt AS (SELECT {GEOG_POINT} AS g)
        SELECT t.id, t.name, t.kind, t.source, t.url, t.center_lat, t.center_lng, t.geojson,
               ST_Distance(t.geom, pt.g) / 1000.0 AS dist_km
        FROM trails t, pt
        WHERE t.kind IN ('path', 'route')
          AND t.geom IS NOT NULL AND ST_DWithin(t.geom, pt.g, %s)
        ORDER BY t.geom <-> pt.g
        LIMIT 1
        """
    row = con.execute(sql, [lng, lat, max_km * 1000.0]).fetchone()
    if row is None:
        return None
    trail_id, name, kind, source, url, clat, clng, geojson, dist = row
    return Trail(
        id=trail_id,
        name=name,
        kind=kind,
        source=source,
        url=url,
        center_lat=clat,
        center_lng=clng,
        distance_km=round(dist, 1),
        camp_distance_km=None,
        geometry=json.loads(geojson),
    )


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
            WHERE region_id = %s AND {taxon_filter(taxon_ids)}
            """,
        ),
        [region_id, *taxon_ids],
    ).fetchall()
    genera = genus_name_map(con, {row[1] for row in rows})
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
    offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """Most recent observations in a region, newest first - the source list for photo thumbnails.

    Fetches one extra row beyond ``limit`` to cheaply detect whether a further page exists
    (issue #174), rather than a separate ``COUNT(*)`` query - trimmed back to ``limit`` before
    returning the ``(observations, has_more)`` pair. ``id`` is a tie-breaker in the ORDER BY since
    ``observed_on`` alone isn't unique - without it, LIMIT/OFFSET paging can skip or repeat rows
    whenever two observations share a date and land on opposite sides of a page boundary.
    """
    binned = BINNED.format(cell=cell_deg)
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            SELECT o.id, o.taxon_id, o.observed_on, o.place_guess, o.uri, o.obscured
            FROM ({binned}) o
            WHERE o.region_id = %s AND {taxon_filter(taxon_ids, "o.taxon_id")} AND o.month IN ({sql_in(months)})
            ORDER BY o.observed_on DESC, o.id DESC
            LIMIT %s OFFSET %s
            """,
        ),
        [region_id, *taxon_ids, *months, limit + 1, offset],
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    genera = genus_name_map(con, {row[1] for row in rows})
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
    return results, has_more


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
    binned = BINNED.format(cell=cell_deg)
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
                SELECT region_id, {CENTER_LAT} AS center_lat, {CENTER_LNG} AS center_lng
                FROM ({binned})
                WHERE observed_on >= %s AND {taxon_filter(taxon_ids)}
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
            WHERE observed_on >= %s AND {taxon_filter(taxon_ids)}
            GROUP BY region_id, taxon_id
            """,
        ),
        [cutoff, *taxon_ids],
    ).fetchall()
    genera = genus_name_map(con, {row[1] for row in rows})

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
    recent_rain = region_precip(con, by_region.keys())
    fires = fire_near(con, lat=home_lat, lng=home_lng, radius_km=radius_km + 30.0) if by_region else []
    for region_id, entry in by_region.items():
        rain = recent_rain.get(region_id, {})
        entry["precip_recent_7d_mm"] = rain.get("precip_7d_mm")
        entry["precip_recent_14d_mm"] = rain.get("precip_14d_mm")
        entry["precip_recent_30d_mm"] = rain.get("precip_30d_mm")
        # `fires` carries distance_km relative to home; re-measure to this region and copy each
        # hit so the card shows the distance from the region, not from home.
        region_fires: list[FireNear] = []
        for fire in fires:
            gap = haversine_km(entry["center_lat"], entry["center_lng"], fire.center_lat, fire.center_lng)
            if gap <= 30.0:
                region_fires.append(replace(fire, distance_km=round(gap, 1)))
        entry["fire_nearby"] = sorted(region_fires, key=lambda fire: fire.distance_km)[:5]
    results = list(by_region.values())
    results.sort(key=lambda region: region["total"], reverse=True)
    return results


def precise_observations(
    con: psycopg.Connection,
    *,
    taxon_ids: list[int],
    lat: float,
    lng: float,
    radius_km: float,
    months: list[int],
) -> list[dict[str, Any]]:
    """Individually-plottable observations within ``radius_km`` of ``lat``/``lng`` whose cached
    coordinate is known-precise (``obscured = false``, i.e. live-verified against iNat, not a
    randomized geoprivacy decoy - see ``ingest.resync``). Everything ``NULL``/``true`` stays out
    of this path entirely and is only ever shown via the coarse region circle (issue #161) - this
    query never widens what a caller already sees, since ``obscured = false`` is exactly the
    subset iNat itself already publishes as an exact point.

    Index-backed ``ST_DWithin`` on ``geom`` for the radius cut (issue #268), and
    (unlike the original version) called with a *destination's* coordinates rather than home's -
    the frontend scopes this to whichever region is currently focused (see map.ts's
    ``regionRadiusKm``), not the whole search radius. No row cap, matching the ``camps_near``/
    ``trails_near``/``land_near`` precedent (those have never had one) - the old radius-wide
    version's 3000-row cap existed to bound a fetch that could span the entire map at once; a
    single destination's own footprint can't realistically produce that.
    """
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            WITH pt AS (SELECT {GEOG_POINT} AS g)
            SELECT o.id, o.taxon_id, o.lat, o.lng, o.observed_on, o.uri
            FROM observations o, pt
            WHERE o.quality_grade = 'research' AND o.obscured = FALSE
              AND o.geom IS NOT NULL AND ST_DWithin(o.geom, pt.g, %s)
              AND {taxon_filter(taxon_ids)} AND o.month IN ({sql_in(months)})
            ORDER BY o.observed_on DESC
            """,
        ),
        [lng, lat, radius_km * 1000.0, *taxon_ids, *months],
    ).fetchall()
    genera = genus_name_map(con, {row[1] for row in rows})

    results = []
    for obs_id, taxon_id, obs_lat, obs_lng, observed_on, uri in rows:
        name, common_name = genera.get(taxon_id, (str(taxon_id), None))
        results.append(
            {
                "id": obs_id,
                "taxon_id": taxon_id,
                "name": name,
                "common_name": common_name,
                "lat": obs_lat,
                "lng": obs_lng,
                "observed_on": observed_on.isoformat() if observed_on else None,
                "uri": uri,
            }
        )
    return results
