"""The read repository: point-and-radius lookups over the cached layers.

``camps_near`` / ``land_near`` / ``trails_near`` / ``nearest_trail`` / ``get_trail`` read the
ingested camp / public-land / trail caches; ``place_calendar`` / ``recent_observations`` /
``alerts`` / ``precise_observations`` read ``phenology`` / ``observations``. All of them return
an empty result when nothing is ingested yet, matching each other.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, LiteralString, cast

import psycopg

from foray.cache import region_precip
from foray.geo import bbox_around, haversine_km
from foray.scoring._sql import (
    BINNED,
    CENTER_LAT,
    CENTER_LNG,
    genus_name_map,
    sql_in,
    taxon_filter,
)
from foray.scoring.models import CampSite, LandUnit, Trail

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
    keeps just those (it never guesses that an unpriced site is free). A cheap bbox
    prefilter in SQL (same technique as ``land_near``/``trails_near``) narrows candidates
    before the exact ``haversine_km`` cut in Python - `campsites` has no bbox columns of its
    own (it's points, not polygons), so the filter is directly against `lat`/`lng`. No rows
    ingested yet yields an empty list, mirroring the other modes. ``limit`` caps the ranked
    result after sorting, mirroring ``trails_near``.
    """
    bbox = bbox_around(lat, lng, radius_km)
    rows = con.execute(
        """
        SELECT id, name, kind, fee, free, lat, lng, source, url FROM campsites
        WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
        """,
        [bbox.min_lat, bbox.max_lat, bbox.min_lng, bbox.max_lng],
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
    sites = [site for _, _, site in scored]
    return sites[:limit] if limit is not None else sites


def land_near(con: psycopg.Connection, *, lat: float, lng: float, radius_km: float) -> list[LandUnit]:
    """Public-land ownership polygons whose bounding box overlaps the home disk.

    Filtering is a cheap bbox-vs-envelope overlap in SQL (the stored geometry needs no spatial
    types); it's coarse on purpose - the map just shades approximate ownership. No rows
    ingested yet yields an empty list, mirroring ``camps_near``.
    """
    bbox = bbox_around(lat, lng, radius_km)
    rows = con.execute(
        """
        SELECT id, agency, unit, source, url, geojson FROM public_land
        WHERE min_lat <= %s AND max_lat >= %s AND min_lng <= %s AND max_lng >= %s
        """,
        [bbox.max_lat, bbox.min_lat, bbox.max_lng, bbox.min_lng],
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
    """Trails whose representative point is within ``radius_km`` of a hotspot, nearest first.

    A cheap bbox-vs-envelope prefilter in SQL (the stored geometry needs no spatial types)
    narrows candidates; the exact cut and ordering use ``haversine_km`` on each trail's stored
    center. Each trail is annotated with the distance to the nearest cached campsite so the UI can
    show the "park → hike → fungi" chain - unless ``with_camp_distance`` is False, which skips that
    O(trails-in-bbox * all-campsites) scan (measured ~13s against a 1M-row/17k-camp production
    cache). ``plan_route`` sets it False: each stop already carries its own selected camp, so a
    second "nearest camp to this trail" figure would be redundant there, and it calls this per stop
    (up to ``max_stops`` times) where the full-catalog scan's cost multiplies fast. ``kind``
    restricts to one element class (e.g. ``"trailhead"`` for the destination-card trail list,
    issue #115 follow-up); ``limit`` caps the ranked result after sorting. No rows ingested yet
    yields an empty list, mirroring ``camps_near`` / ``land_near``.
    """
    bbox = bbox_around(lat, lng, radius_km)
    params: list[Any] = [bbox.max_lat, bbox.min_lat, bbox.max_lng, bbox.min_lng]
    kind_filter = ""
    if kind is not None:
        kind_filter = "AND kind = %s"
        params.append(kind)
    rows = con.execute(
        f"""
        SELECT id, name, kind, source, url, center_lat, center_lng, geojson FROM trails
        WHERE min_lat <= %s AND max_lat >= %s AND min_lng <= %s AND max_lng >= %s {kind_filter}
        """,
        params,
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
    a heuristic, not an authoritative link, so callers should label it as such. Distance is
    point-to-vertex haversine over each candidate's already-thinned geometry (<=60 points per
    line, see ``trails.py``'s ``_MAX_POINTS_PER_LINE``) rather than true point-to-segment distance
    - close enough given the thinning, and avoids a geometry library dependency for one heuristic.
    """
    bbox = bbox_around(lat, lng, max_km)
    rows = con.execute(
        """
        SELECT id, name, kind, source, url, center_lat, center_lng, geojson FROM trails
        WHERE kind IN ('path', 'route')
          AND min_lat <= %s AND max_lat >= %s AND min_lng <= %s AND max_lng >= %s
        """,
        [bbox.max_lat, bbox.min_lat, bbox.max_lng, bbox.min_lng],
    ).fetchall()

    best: tuple[float, Trail] | None = None
    for trail_id, name, kind, source, url, clat, clng, geojson in rows:
        geometry = json.loads(geojson)
        coords = geometry["coordinates"]
        lines = coords if geometry["type"] == "MultiLineString" else [coords]
        vertex_dist = min(haversine_km(lat, lng, vlat, vlng) for line in lines for vlng, vlat in line)
        if vertex_dist > max_km:
            continue
        if best is None or vertex_dist < best[0]:
            best = (
                vertex_dist,
                Trail(
                    id=trail_id,
                    name=name,
                    kind=kind,
                    source=source,
                    url=url,
                    center_lat=clat,
                    center_lng=clng,
                    distance_km=round(vertex_dist, 1),
                    camp_distance_km=None,
                    geometry=geometry,
                ),
            )
    return best[1] if best is not None else None


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
    for region_id, entry in by_region.items():
        rain = recent_rain.get(region_id, {})
        entry["precip_recent_7d_mm"] = rain.get("precip_7d_mm")
        entry["precip_recent_14d_mm"] = rain.get("precip_14d_mm")
        entry["precip_recent_30d_mm"] = rain.get("precip_30d_mm")
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

    Same bbox-prefilter-then-haversine-cut technique as ``camps_near``/``trails_near``, and
    (unlike the original version) called with a *destination's* coordinates rather than home's -
    the frontend scopes this to whichever region is currently focused (see map.ts's
    ``regionRadiusKm``), not the whole search radius. No row cap, matching the ``camps_near``/
    ``trails_near``/``land_near`` precedent (those have never had one) - the old radius-wide
    version's 3000-row cap existed to bound a fetch that could span the entire map at once; a
    single destination's own footprint can't realistically produce that.
    """
    bbox = bbox_around(lat, lng, radius_km)
    rows = con.execute(
        cast(
            LiteralString,
            f"""
            SELECT o.id, o.taxon_id, o.lat, o.lng, o.observed_on, o.uri
            FROM observations o
            WHERE o.quality_grade = 'research' AND o.obscured = FALSE
              AND o.lat BETWEEN %s AND %s AND o.lng BETWEEN %s AND %s
              AND {taxon_filter(taxon_ids)} AND o.month IN ({sql_in(months)})
            ORDER BY o.observed_on DESC
            """,
        ),
        [bbox.min_lat, bbox.max_lat, bbox.min_lng, bbox.max_lng, *taxon_ids, *months],
    ).fetchall()
    genera = genus_name_map(con, {row[1] for row in rows})

    results = []
    for obs_id, taxon_id, obs_lat, obs_lng, observed_on, uri in rows:
        dist = haversine_km(lat, lng, obs_lat, obs_lng)
        if dist > radius_km:
            continue
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
