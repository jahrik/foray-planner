"""The read repository: point-and-radius lookups over the cached layers.

``camps_near`` / ``land_near`` / ``trails_near`` / ``nearest_trail`` / ``get_trail`` read the
ingested camp / public-land / trail caches; ``place_calendar`` / ``recent_observations`` /
``alerts`` / ``precise_observations`` read ``phenology`` / ``observations``. All of them return
an empty result when nothing is ingested yet, matching each other.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Literal, LiteralString, cast

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
        SELECT id, name, kind, fee, free, lat, lng, source, url, reservable, fee_low, fee_high,
               ST_Distance(c.geom, pt.g) / 1000.0 AS dist_km
        FROM campsites c, pt
        WHERE c.geom IS NOT NULL AND ST_DWithin(c.geom, pt.g, %s)
        """
    rows = con.execute(sql, [lng, lat, radius_km * 1000.0]).fetchall()

    # Keep the unrounded distance alongside each site so ranking is exact; distance_km is
    # only rounded for display and must not be the sort key (near-equal sites would tie).
    scored: list[tuple[bool, float, CampSite]] = []
    for site_id, name, kind, fee, free, site_lat, site_lng, source, url, reservable, fee_low, fee_high, dist in rows:
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
            reservable=reservable,
            fee_low=fee_low,
            fee_high=fee_high,
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


TrailSort = Literal["nearest", "relevance", "longest"]

# A trailhead / path is worth listing (vs. leaving as a thin line on the map) if it is named, is
# part of a hiking route, or runs at least this far - keeps the list clear of OSM's 50-200 m
# connector stubs (issue #306). Synthetic names all end in "(OSM)".
_SIGNIFICANT_LENGTH_KM = 0.5
_ROUTE_RELEVANCE_BONUS = 3.0  # a trailhead whose trail is a named route sorts well above a spur
# Target-genus observations within this of the trail line push it up the relevance sort - a
# trail that runs through where the mushrooms are is the point (issue #306). Log-scaled so a
# handful of finds matters but a hotspot doesn't swamp length/route entirely.
_OBS_RELEVANCE_RADIUS_M = 500
_OBS_RELEVANCE_WEIGHT = 2.5
# For ``kind='road'`` obs-density *is* the ranking: an old forest road is worth walking because
# the mushrooms fruit along it, not because it is long or named. So roads get a much heavier obs
# weight and their length is log-damped (a 20 km road with no finds shouldn't outrank a 2 km one
# that runs through a hotspot).
_ROAD_OBS_RELEVANCE_WEIGHT = 6.0
_ROAD_LENGTH_WEIGHT = 1.0
# A forest road closed to motor vehicles but open on foot is prime foraging - walk-in, less
# picked - so being gated is a positive signal here, not the access penalty it looks like.
_WALK_IN_RELEVANCE_BONUS = 2.0
# Values of ``motor_vehicle`` / ``access`` that keep the general public from *driving* in.
_CLOSED_TO_PUBLIC = frozenset({"no", "private", "permit", "forestry", "agricultural", "delivery", "military"})
# ``foot`` values that positively grant walking access (needed to override a blanket ``access=*``,
# which by OSM convention closes every mode including foot).
_FOOT_ALLOWED = frozenset({"yes", "permissive", "designated", "official", "customers", "permit"})
_FOOT_DENIED = frozenset({"no", "private"})


def _walk_in(attrs: dict[str, str] | None) -> bool:
    """True for a forest road the public can't *drive* but can still walk.

    ``motor_vehicle`` restricts only vehicles, so a ``motor_vehicle=no`` road is walk-in unless
    ``foot`` explicitly denies it. A blanket ``access=*`` closes every mode by OSM convention, so
    that only counts as walk-in when ``foot`` is explicitly re-granted.
    """
    if not attrs:
        return False
    foot = attrs.get("foot")
    if foot in _FOOT_DENIED:
        return False
    if attrs.get("access") in _CLOSED_TO_PUBLIC:
        return foot in _FOOT_ALLOWED
    return attrs.get("motor_vehicle") in _CLOSED_TO_PUBLIC


def trails_near(
    con: psycopg.Connection,
    *,
    lat: float,
    lng: float,
    radius_km: float,
    kind: str | None = None,
    limit: int | None = None,
    sort: TrailSort = "nearest",
    significant_only: bool = False,
    taxon_ids: list[int] | None = None,
    with_camp_distance: bool = True,
    with_geometry: bool = True,
) -> list[Trail]:
    """Trails within ``radius_km`` of a hotspot.

    ``sort`` orders the result: ``"nearest"`` (default, unchanged) by point-to-trail distance;
    ``"relevance"`` by a trail-prominence score (part of a named route, then the length of the
    trail the row leads to, then target-genus observations hugging the line) with distance as the
    tiebreak; ``"longest"`` by that length. For ``kind='road'`` rows the relevance score is
    re-weighted (see ``_ROAD_OBS_RELEVANCE_WEIGHT``): obs-density dominates, length is log-damped,
    and a gated-but-walkable road (``_walk_in``) gets a bonus.
    ``relevance`` / ``longest`` can't use the KNN pre-limit, so they fetch every trail in the
    radius (hard-capped) then sort. ``significant_only`` drops rows that are an unnamed
    sub-0.5 km stub with no route - the OSM connector noise the Details list shouldn't show.

    Index-backed ``ST_DWithin`` on ``geom`` for the radius cut; ``ST_Distance`` gives the exact
    point-to-trail distance (issue #268 - previously a bbox prefilter then a ``haversine_km``
    cut on each trail's stored center). Each trail is annotated with the distance to the nearest
    cached campsite (``camp_distance_km``) so the UI can show the "park -> hike -> fungi" chain -
    a ``LATERAL`` KNN join off ``ix_campsites_geom`` (issue #268 PR 5 - previously an
    O(trails-in-radius * all-campsites) Python loop, ~13s on a 1M-trail / 17k-camp prod cache).

    ``kind`` restricts to one element class (e.g. ``"trailhead"`` for the destination-card trail
    list, issue #115 follow-up). ``limit`` caps the result - and when set, is pushed into the
    query as ``ORDER BY <-> LIMIT`` so only that many trails are fetched (and camp-joined),
    rather than every trail in the radius then trimmed. ``with_camp_distance=False`` drops the
    per-trail nearest-camp LATERAL entirely - the trip planner only wants the single nearest
    trail's geometry and does its own ``camps_near``, and that join still costs seconds per
    call in trail-dense terrain. No rows ingested yet yields an empty list.

    ``with_geometry=False`` drops each trail's GeoJSON from the result (``geometry`` is ``None``).
    The ``/api/trails`` list only shows names + distances and fetches real geometry per row via
    ``/api/trails/network``, so shipping every LineString there is megabytes of unused payload.
    """
    # params are appended in the order their %s appears in the final SQL: GEOG_POINT (CTE) ->
    # obs_join (FROM) -> radius (WHERE) -> kind (WHERE) -> limit (ORDER BY).
    params: list[Any] = [lng, lat]
    geojson_select: LiteralString = "t.geojson" if with_geometry else "NULL::text"
    kind_filter: LiteralString = "AND t.kind = %s" if kind is not None else ""
    if with_camp_distance:
        camp_select: LiteralString = "camp.d / 1000.0 AS camp_km"
        camp_join: LiteralString = """
        LEFT JOIN LATERAL (
            SELECT ST_Distance(c.geom, t.geom) AS d
            FROM campsites c
            WHERE c.geom IS NOT NULL
            ORDER BY c.geom <-> t.geom
            LIMIT 1
        ) camp ON true"""
    else:
        camp_select = "NULL::double precision AS camp_km"
        camp_join = ""
    # A trailhead row's "prominence" comes from the trail it leads to (its ``connects`` list); a
    # path/route row's from itself. ``lead`` carries the best connected trail's length + whether
    # any is a route so ``relevance`` / ``longest`` can rank on it - only joined when sorting on it.
    if sort == "nearest":
        lead_join: LiteralString = ""
        lead_select: LiteralString = "NULL::double precision AS lead_len, NULL::boolean AS has_route"
    else:
        lead_join = """
        LEFT JOIN LATERAL (
            SELECT sum(ct.length_km) AS lead_len, bool_or(ct.kind = 'route') AS has_route
            FROM trails ct WHERE t.connects IS NOT NULL AND ct.id = ANY(t.connects)
        ) lead ON true"""
        lead_select = "lead.lead_len, lead.has_route"
    # Foraging-relevance term: count target-genus observations hugging the trail line. Only for a
    # relevance sort with a genus filter - "all genera" would just count every fungus everywhere.
    if sort == "relevance" and taxon_ids:
        obs_join: LiteralString = cast(
            LiteralString,
            f"""
        LEFT JOIN LATERAL (
            SELECT count(*) AS n FROM observations o
            WHERE o.geom IS NOT NULL AND ST_DWithin(o.geom, t.geom, {_OBS_RELEVANCE_RADIUS_M})
              AND o.quality_grade = 'research' AND {taxon_filter(taxon_ids, "o.taxon_id")}
        ) obs ON true""",
        )
        obs_select: LiteralString = "obs.n"
        params.extend(taxon_ids)
    else:
        obs_join = ""
        obs_select = "0::bigint"

    params.append(radius_km * 1000.0)
    if kind is not None:
        params.append(kind)

    order_limit: LiteralString = ""
    if sort == "nearest" and limit is not None:
        # geography `<->` is true spherical distance in PostGIS >= 2.2 (same as ST_Distance,
        # not a centroid approximation), so the KNN pre-limit picks the genuine nearest N -
        # the Python re-sort below just orders them. Same pattern as `nearest_trail`.
        order_limit = "ORDER BY t.geom <-> pt.g LIMIT %s"
        params.append(limit)
    elif sort != "nearest":
        # relevance / longest need every candidate before sorting; cap so a trail-dense radius
        # can't return a pathological row count.
        order_limit = "ORDER BY t.geom <-> pt.g LIMIT 500"
    sql: LiteralString = f"""
        WITH pt AS (SELECT {GEOG_POINT} AS g)
        SELECT t.id, t.name, t.kind, t.source, t.url, t.center_lat, t.center_lng, {geojson_select} AS geojson,
               t.length_km, t.attrs,
               ST_Distance(t.geom, pt.g) / 1000.0 AS dist_km,
               {camp_select},
               {lead_select},
               {obs_select} AS obs_n
        FROM trails t, pt{camp_join}{lead_join}{obs_join}
        WHERE t.geom IS NOT NULL AND ST_DWithin(t.geom, pt.g, %s) {kind_filter}
        {order_limit}
        """
    rows = con.execute(sql, params).fetchall()

    # row layout: [8] this row's own length_km, [9] attrs, [10] unrounded distance, [11] camp
    # distance, [12] best connected length, [13] connects-a-route flag, [14] target-genus obs
    # count near the line. Prominence is the lead trail's length (for a trailhead) or the row's
    # own (for a path), plus a route bonus, plus a log-scaled foraging-density term.
    def lead_length(row: Sequence[Any]) -> float:
        return row[12] if row[12] is not None else (row[8] or 0.0)

    def significant(row: Sequence[Any]) -> bool:
        name, row_kind = row[1], row[2]
        named = name is not None and not name.endswith("(OSM)")
        if row_kind == "trailhead":
            # "Trailhead (OSM) - 16 mi" tells the user nothing whatever it connects to, so an
            # unnamed trailhead never makes the list (it's still drawn on the map).
            return named
        # A path / route / road earns its row by being named, being a route, or running far
        # enough - a long unnamed forest road (FR 300 split into ref-only segments) counts on
        # length, same as any path.
        return named or row_kind == "route" or lead_length(row) >= _SIGNIFICANT_LENGTH_KM

    def relevance(row: Sequence[Any]) -> float:
        row_kind = row[2]
        attrs = json.loads(row[9]) if row[9] else None
        route_bonus = _ROUTE_RELEVANCE_BONUS if (row[13] or row_kind == "route") else 0.0
        if row_kind == "road":
            obs_bonus = _ROAD_OBS_RELEVANCE_WEIGHT * math.log1p(row[14] or 0)
            length_term = _ROAD_LENGTH_WEIGHT * math.log1p(lead_length(row))
            walk_in_bonus = _WALK_IN_RELEVANCE_BONUS if _walk_in(attrs) else 0.0
            return length_term + route_bonus + obs_bonus + walk_in_bonus
        obs_bonus = _OBS_RELEVANCE_WEIGHT * math.log1p(row[14] or 0)
        return lead_length(row) + route_bonus + obs_bonus

    candidates = [row for row in rows if not significant_only or significant(row)]
    if sort == "relevance":
        candidates.sort(key=lambda row: (-relevance(row), row[10]))
    elif sort == "longest":
        candidates.sort(key=lambda row: (-lead_length(row), row[10]))
    else:
        # Rank on the unrounded distance so near-ties keep their true order (matches ``camps_near``).
        candidates.sort(key=lambda row: row[10])
    if kind == "trailhead":
        # Several nodes often share a trailhead name (different access points to one park); keep
        # only the best-ranked of each so the list isn't three "Beaver Pond Natural Area" rows.
        seen: set[str] = set()
        candidates = [row for row in candidates if not (row[1] in seen or seen.add(row[1]))]
    trails = [_base_trail(row, distance_km=row[10], camp_distance_km=row[11]) for row in candidates]
    return trails[:limit] if limit is not None else trails


def _base_trail(row: Sequence[Any], *, distance_km: float, camp_distance_km: float | None) -> Trail:
    """Build a ``Trail`` from the standard 10-column prefix
    ``(id, name, kind, source, url, center_lat, center_lng, geojson, length_km, attrs)``."""
    tid, name, kind, source, url, clat, clng, geojson, length_km, attrs = row[:10]
    parsed_attrs = json.loads(attrs) if attrs else None
    return Trail(
        id=tid,
        name=name,
        kind=kind,
        source=source,
        url=url,
        center_lat=clat,
        center_lng=clng,
        distance_km=round(distance_km, 1),
        camp_distance_km=round(camp_distance_km, 1) if camp_distance_km is not None else None,
        geometry=json.loads(geojson) if geojson else None,
        length_km=length_km,
        attrs=parsed_attrs,
        walk_in=kind == "road" and _walk_in(parsed_attrs),
    )


def get_trail(con: psycopg.Connection, trail_id: str) -> Trail | None:
    """Single trail row by id, or None if not cached. No camp-distance annotation (see ``trails_near``)."""
    row = con.execute(
        "SELECT id, name, kind, source, url, center_lat, center_lng, geojson, connects, length_km, attrs "
        "FROM trails WHERE id = %s",
        [trail_id],
    ).fetchone()
    if row is None:
        return None
    trail_id_, name, kind, source, url, clat, clng, geojson, connects, length_km, attrs = row
    parsed_attrs = json.loads(attrs) if attrs else None
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
        connects=connects,
        length_km=length_km,
        attrs=parsed_attrs,
        walk_in=kind == "road" and _walk_in(parsed_attrs),
    )


def connected_trails(con: psycopg.Connection, trail_ids: Sequence[str]) -> list[Trail]:
    """The path/route trails named in a trailhead's ``connects`` list, geometry included.

    Feeds ``trails.resolve_trail_network``: the ingest-time spatial link (issue #306) recorded
    which trails a trailhead node touches, and this reads them back so the selection draws from
    cache. Order follows ``trail_ids``; ids no longer in the cache are silently dropped.
    """
    if not trail_ids:
        return []
    rows = con.execute(
        "SELECT id, name, kind, source, url, center_lat, center_lng, geojson, length_km, attrs "
        "FROM trails WHERE id = ANY(%s)",
        [list(trail_ids)],
    ).fetchall()
    by_id = {row[0]: _base_trail(row, distance_km=0.0, camp_distance_km=None) for row in rows}
    return [by_id[tid] for tid in trail_ids if tid in by_id]


# Beyond this, "no trailhead nearby" and "this area isn't mapped yet" are indistinguishable, so
# region_access returns None (unknown - no score effect) rather than a distance that would
# always trip the remote penalty. Also bounds the KNN so a stale row from a prior home / a
# different refresh area can't be the "nearest" match (Copilot review, PR #307).
_ACCESS_SEARCH_KM = 45.0


def region_access(
    con: psycopg.Connection, regions: Sequence[tuple[str, float, float]]
) -> dict[str, tuple[float | None, float | None, bool | None]]:
    """Nearest trailhead / campground to each ``(region_id, lat, lng)`` in km (within
    ``_ACCESS_SEARCH_KM``), plus whether that nearest campsite is free-tagged.

    One batched KNN pass off ``ix_trails_geom`` / ``ix_campsites_geom`` - feeds the ``access``
    multiplier and the card why-sentence (issue #306). A region with nothing cached within the
    search radius comes back ``None`` in that slot - treated as "unknown", not "remote".
    """
    if not regions:
        return {}
    values = ", ".join(["(%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography)"] * len(regions))
    params: list[Any] = []
    for region_id, lat, lng in regions:
        params += [region_id, lng, lat]
    params.append(_ACCESS_SEARCH_KM * 1000.0)
    params.append(_ACCESS_SEARCH_KM * 1000.0)
    sql: LiteralString = f"""
        WITH r(id, g) AS (VALUES {values})
        SELECT r.id, th.dist_km, camp.dist_km, camp.free
        FROM r
        LEFT JOIN LATERAL (
            SELECT ST_Distance(t.geom, r.g) / 1000.0 AS dist_km
            FROM trails t
            WHERE t.kind = 'trailhead' AND t.geom IS NOT NULL AND ST_DWithin(t.geom, r.g, %s)
            ORDER BY t.geom <-> r.g LIMIT 1
        ) th ON true
        LEFT JOIN LATERAL (
            SELECT ST_Distance(c.geom, r.g) / 1000.0 AS dist_km, c.free
            FROM campsites c WHERE c.geom IS NOT NULL AND ST_DWithin(c.geom, r.g, %s)
            ORDER BY c.geom <-> r.g LIMIT 1
        ) camp ON true
        """
    rows = con.execute(sql, params).fetchall()
    return {
        rid: (
            round(th_km, 1) if th_km is not None else None,
            round(camp_km, 1) if camp_km is not None else None,
            camp_free,
        )
        for rid, th_km, camp_km, camp_free in rows
    }


def nearest_trail(con: psycopg.Connection, *, lat: float, lng: float, max_km: float = 2.0) -> Trail | None:
    """Nearest cached path/road/route to (``lat``, ``lng``), or None if nothing is within ``max_km``.

    Fallback for ``trails.resolve_trail_network`` (issue: "draw the real trail on trailhead
    selection") when OSM has no topological link between a trailhead node and any way/relation -
    a heuristic, not an authoritative link, so callers should label it as such. ``ST_DWithin`` +
    ``geom <-> pt`` KNN sort give true point-to-line distance off the GIST index (issue #268 -
    previously a point-to-vertex haversine over the thinned geometry).
    """
    sql: LiteralString = f"""
        WITH pt AS (SELECT {GEOG_POINT} AS g)
        SELECT t.id, t.name, t.kind, t.source, t.url, t.center_lat, t.center_lng, t.geojson,
               t.length_km, t.attrs, ST_Distance(t.geom, pt.g) / 1000.0 AS dist_km
        FROM trails t, pt
        WHERE t.kind IN ('path', 'road', 'route')
          AND t.geom IS NOT NULL AND ST_DWithin(t.geom, pt.g, %s)
        ORDER BY t.geom <-> pt.g
        LIMIT 1
        """
    row = con.execute(sql, [lng, lat, max_km * 1000.0]).fetchone()
    if row is None:
        return None
    return _base_trail(row, distance_km=row[10], camp_distance_km=None)


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
