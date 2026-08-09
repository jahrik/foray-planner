"""Trail layer from OpenStreetMap (Overpass API).

Epic 3's question is "shortest walk from where I can park to where they're fruiting", so this
module pulls the walkable network near home from OSM and caches it as ``trails`` rows the map and
scoring read directly. One ODbL-licensed Overpass query gathers three element classes:

* **Paths** (``kind='path'``) - backcountry/trail ways (``highway=path``), cached as a
  ``LineString`` polyline. We deliberately *exclude* ``highway=footway``: it is dominated by urban
  sidewalks (measured ~6x the row count over a wide radius - e.g. 44.7k vs 7.4k ways at 200 km),
  which is noise for a mushroom-trail planner and heavy enough to time the full-radius query out
  on public Overpass. ``highway=path`` is the tag that actually maps forest trails.
* **Hiking routes** (``kind='route'``) - named long trails (``route=hiking`` relations), cached as
  a ``MultiLineString`` stitched from their member ways.
* **Trailheads** (``kind='trailhead'``) - where you actually start walking (``highway=trailhead``
  nodes), cached as a ``Point``.

Geometry is stored as GeoJSON *text* with a bounding box + a representative center point (see
``cache.trails``), so the read path never needs PostGIS geometry types - a cheap bbox filter +
haversine on the center serves "trails near here". Each way's vertices are thinned to keep the
cached polylines light enough for a phone map.

Like the campground, land, and dispersed ingests, this is best-effort: a failing Overpass request
is logged and skipped rather than aborting the whole refresh. It is informational only - it links
the OSM source and makes no legal-access claim (see AGENTS.md, "No claims").

Scale note: even ``highway=path`` alone grows with radius (~7.4k ways at 200 km around Coos Bay,
~15k at the full 400 km), but stays inside Overpass's server budget as a single query. If a future
radius pushes past that, tile the home disk into sub-queries the way ``camps.py`` does - the read
path (bbox + haversine to the hotspot) is unaffected either way.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import psycopg

from foray import scoring
from foray.cache import connect, is_area_covered, is_ingested, record_ingest, upsert_trails
from foray.config import Config, CoverageRegion

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "foray-planner/0.1 (mushroom trip planner; +https://github.com/jahrik)"

# A single path can carry hundreds of vertices; the map only needs enough to trace its shape, so
# each line is thinned to at most this many evenly-spaced points before caching.
_MAX_POINTS_PER_LINE = 60

# A whole state's Overpass response (with full geometry for every path/route/trailhead) can be
# large enough to OOM a small droplet before it's even parsed - a well-mapped state like Arizona
# killed a 1 GB box mid-response. Tiling each region into degree-sized sub-queries bounds a
# single response's size regardless of how big or trail-dense the region is; each tile's rows
# are upserted and discarded before the next tile starts; see ``ingest_trails_region``.
_TILE_DEG = 2.0


def _tile_bboxes(
    min_lat: float, min_lng: float, max_lat: float, max_lng: float, tile_deg: float = _TILE_DEG
) -> list[tuple[float, float, float, float]]:
    """Carve a bbox into a grid of (min_lat, min_lng, max_lat, max_lng) tiles <= tile_deg wide."""
    if tile_deg <= 0:
        raise ValueError(f"tile_deg must be positive, got {tile_deg}")
    tiles: list[tuple[float, float, float, float]] = []
    lat = min_lat
    while lat < max_lat:
        lat_end = min(lat + tile_deg, max_lat)
        lng = min_lng
        while lng < max_lng:
            lng_end = min(lng + tile_deg, max_lng)
            tiles.append((lat, lng, lat_end, lng_end))
            lng = lng_end
        lat = lat_end
    return tiles


def _around(lat: float, lng: float, radius_m: float) -> str:
    return f"around:{radius_m:.0f},{lat},{lng}"


def _trails_query(lat: float, lng: float, radius_m: float) -> str:
    """Overpass QL for walkable paths, hiking routes, and trailheads within the home disk."""
    around = _around(lat, lng, radius_m)
    return (
        "[out:json][timeout:180];"
        "("
        f'way["highway"="path"]({around});'
        f'relation["route"="hiking"]({around});'
        f'node["highway"="trailhead"]({around});'
        ");"
        "out geom tags;"
    )


def _network_query(node_id: int, *, timeout_s: int = 25) -> str:
    """Overpass QL for the way(s)/route touching a specific trailhead node (issue: draw the real
    trail on selection, not a proximity guess).

    ``way(bn)`` ("by node") returns ways that have ``node_id`` as a member - the real OSM link
    when a trailhead sits on its path's own vertex list, not always true (many trailheads are a
    standalone POI near, not on, the path - see ``trails.resolve_trail_network``'s fallback for
    that case). ``rel(bw.segs)`` ("by way") then pulls in any named ``route=hiking`` relation
    those ways belong to, so a long-distance trail draws in full rather than one short segment.
    """
    return (
        f"[out:json][timeout:{timeout_s}];"
        f"node(id:{node_id});"
        'way(bn)["highway"]->.segs;'
        '(.segs; rel(bw.segs)["route"="hiking"];);'
        "out geom tags;"
    )


def _bbox_filter(min_lat: float, min_lng: float, max_lat: float, max_lng: float) -> str:
    return f"({min_lat},{min_lng},{max_lat},{max_lng})"


def _trails_query_bbox(min_lat: float, min_lng: float, max_lat: float, max_lng: float, *, timeout_s: int = 300) -> str:
    """Overpass QL for the same three element classes within a state-sized bbox.

    A whole state (rather than a home-radius circle) is large enough that the query needs a
    longer server-side timeout - Overpass rejects a query outright if its own [timeout:N] is
    exceeded, so this defaults higher than the home-radius query's 180s.
    """
    bbox = _bbox_filter(min_lat, min_lng, max_lat, max_lng)
    return (
        f"[out:json][timeout:{timeout_s}];"
        "("
        f'way["highway"="path"]{bbox};'
        f'relation["route"="hiking"]{bbox};'
        f'node["highway"="trailhead"]{bbox};'
        ");"
        "out geom tags;"
    )


def _post_overpass(client: httpx.Client, query: str, *, attempts: int = 4, base_delay: float = 2.0) -> dict[str, Any]:
    """POST a query, backing off on Overpass's throttle (429) / timeout (504) responses."""
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    resp: httpx.Response | None = None
    for attempt in range(1, attempts + 1):
        resp = client.post(OVERPASS_URL, data={"data": query}, headers={"User-Agent": USER_AGENT})
        if resp.status_code in (429, 504) and attempt < attempts:
            retry_after = resp.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else base_delay * 2 ** (attempt - 1)
            time.sleep(delay)
            continue
        break
    assert resp is not None
    resp.raise_for_status()
    return resp.json()


def _line_coords(geometry: Sequence[dict[str, Any]]) -> list[tuple[float, float]]:
    """(lat, lng) vertices of an Overpass `geometry` array, dropping malformed nodes."""
    return [
        (float(node["lat"]), float(node["lon"]))
        for node in geometry
        if node.get("lat") is not None and node.get("lon") is not None
    ]


def _sample(coords: Sequence[tuple[float, float]], count: int) -> list[tuple[float, float]]:
    """Thin a vertex list to at most ``count`` evenly-spaced points, keeping first and last."""
    if len(coords) <= count:
        return list(coords)
    step = (len(coords) - 1) / (count - 1)
    return [coords[round(index * step)] for index in range(count)]


def _bbox(points: Sequence[tuple[float, float]]) -> tuple[float, float, float, float]:
    """(min_lat, min_lng, max_lat, max_lng) of a non-empty (lat, lng) point list."""
    lats = [lat for lat, _ in points]
    lngs = [lng for _, lng in points]
    return min(lats), min(lngs), max(lats), max(lngs)


def _to_lnglat(coords: Sequence[tuple[float, float]]) -> list[list[float]]:
    """(lat, lng) tuples -> GeoJSON [lng, lat] pairs (GeoJSON is x=lng, y=lat)."""
    return [[lng, lat] for lat, lng in coords]


def _trail_url(etype: str, eid: int) -> str:
    return f"https://www.openstreetmap.org/{etype}/{eid}"


def _row(
    etype: str,
    eid: int,
    name: str,
    kind: str,
    lines: Sequence[Sequence[tuple[float, float]]],
) -> tuple[Any, ...] | None:
    """Build a trails row from one or more (lat, lng) polylines, or None if all are empty.

    A lone vertex (a trailhead node) becomes a ``Point``; a single line a ``LineString``; several
    a ``MultiLineString``. The center is the middle vertex of the concatenated geometry so it
    lands on the trail, not in its bbox gap.
    """
    thinned = [_sample(line, _MAX_POINTS_PER_LINE) for line in lines if line]
    flat = [point for line in thinned for point in line]
    if not flat:
        return None
    if len(flat) == 1:
        lone_lat, lone_lng = flat[0]
        geometry: dict[str, Any] = {"type": "Point", "coordinates": [lone_lng, lone_lat]}
    elif len(thinned) == 1:
        geometry = {"type": "LineString", "coordinates": _to_lnglat(thinned[0])}
    else:
        geometry = {
            "type": "MultiLineString",
            "coordinates": [_to_lnglat(line) for line in thinned],
        }
    min_lat, min_lng, max_lat, max_lng = _bbox(flat)
    center_lat, center_lng = flat[len(flat) // 2]
    return (
        f"osm:{etype}/{eid}",
        name,
        kind,
        "osm",
        _trail_url(etype, eid),
        min_lat,
        min_lng,
        max_lat,
        max_lng,
        center_lat,
        center_lng,
        json.dumps(geometry, separators=(",", ":")),
    )


def _parse_element(element: dict[str, Any]) -> tuple[Any, ...] | None:
    """One Overpass element -> a trails row tuple, or None if it carries no usable geometry."""
    etype = element.get("type")
    eid = element.get("id")
    if eid is None:
        return None
    tags = element.get("tags") or {}
    if etype == "node":
        lat, lng = element.get("lat"), element.get("lon")
        if lat is None or lng is None:
            return None
        point = (float(lat), float(lng))
        name = tags.get("name") or "Trailhead (OSM)"
        return _row("node", int(eid), name, "trailhead", [[point]])
    if etype == "way":
        coords = _line_coords(element.get("geometry") or [])
        if not coords:
            return None
        name = tags.get("name") or tags.get("ref") or "Trail (OSM)"
        return _row("way", int(eid), name, "path", [coords])
    if etype == "relation":
        # `out geom` returns each way member with its own `geometry`; stitch them into one route.
        lines = [
            coords
            for member in element.get("members") or []
            if member.get("type") == "way" and (coords := _line_coords(member.get("geometry") or []))
        ]
        if not lines:
            return None
        name = tags.get("name") or tags.get("ref") or "Hiking route (OSM)"
        return _row("relation", int(eid), name, "route", lines)
    return None


def _parse_trails(payload: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Overpass payload -> trails rows, deduped by id (paths, hiking routes, trailheads)."""
    by_id: dict[str, tuple[Any, ...]] = {}
    for element in payload.get("elements", []):
        row = _parse_element(element)
        if row is not None:
            by_id[row[0]] = row
    return list(by_id.values())


def fetch_trails(
    *,
    lat: float,
    lng: float,
    radius_km: float,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> list[tuple[Any, ...]]:
    """Fetch OSM paths, hiking routes, and trailheads near home as trails rows.

    Best-effort like the other OSM/ArcGIS ingests: a failing or malformed Overpass response is
    logged and yields ``[]`` rather than aborting the refresh.
    """
    owns = client is None
    client = client or httpx.Client(timeout=180.0)
    radius_m = radius_km * 1000.0
    try:
        if progress_cb:
            progress_cb("Fetching trails…", 50.0)
        payload = _post_overpass(client, _trails_query(lat, lng, radius_m))
        rows = _parse_trails(payload)
        logger.info("trails: %d trails/routes/trailheads", len(rows))
        return rows
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        logger.warning("trails: query failed (%s) - skipping", error)
        return []
    finally:
        if owns:
            client.close()


def ingest_trails(
    cfg: Config,
    con: psycopg.Connection | None = None,
    *,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest the OSM trail network near home into ``trails``. Returns rows upserted."""
    own_con = con is None
    database = con if con is not None else connect()
    home = cfg.home
    if is_area_covered(database, "trails:", home.lat, home.lng, home.radius_km):
        logger.info("trails: already ingested for this area, skipping")
        if progress_cb:
            progress_cb("Trails already cached, skipping…", 100.0)
        if own_con:
            database.close()
        return 0
    try:
        logger.info("trails: fetching OSM trail network within %.0f km of home…", home.radius_km)
        rows = fetch_trails(
            lat=home.lat,
            lng=home.lng,
            radius_km=home.radius_km,
            client=client,
            progress_cb=progress_cb,
        )
        upsert_trails(database, rows)
        key = f"trails:{home.lat}:{home.lng}:{home.radius_km}"
        record_ingest(database, key, len(rows), lat=home.lat, lng=home.lng, radius_km=home.radius_km)
        logger.info("trails: cached %d trails", len(rows))
        return len(rows)
    finally:
        if own_con:
            database.close()


def _parse_trailhead_id(trail_id: str) -> int:
    """``"osm:node/<id>"`` -> the numeric OSM node id, or raise ``ValueError`` for anything else.

    Only trailhead rows should ever reach this - ``resolve_trail_network`` calls it right after
    confirming ``kind == "trailhead"`` via ``scoring.get_trail``.
    """
    prefix = "osm:node/"
    if not trail_id.startswith(prefix) or not trail_id[len(prefix) :].isdigit():
        raise ValueError(f"not a trailhead node id: {trail_id!r}")
    return int(trail_id[len(prefix) :])


def trailhead_network(node_id: int, *, client: httpx.Client | None = None) -> dict[str, Any] | None:
    """The real way/route geometry touching trailhead ``node_id``, or None if OSM has no link.

    Best-effort like ``fetch_trails``: a failed or empty Overpass response yields None rather than
    raising, so callers (``resolve_trail_network``) can fall back to a proximity guess instead of
    breaking the whole selection. Multiple returned ways/route members are stitched into one
    ``MultiLineString`` (same approach as ``_row``'s route-relation handling); a lone way becomes
    a ``LineString``.
    """
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        payload = _post_overpass(client, _network_query(node_id))
    except httpx.HTTPError as error:
        logger.warning("trails: network query failed for node %d (%s) - falling back", node_id, error)
        return None
    finally:
        if owns:
            client.close()

    lines: list[list[tuple[float, float]]] = []
    name = None
    kind = "path"
    for element in payload.get("elements", []):
        tags = element.get("tags") or {}
        if element.get("type") == "way":
            coords = _line_coords(element.get("geometry") or [])
            if coords:
                lines.append(coords)
                name = name or tags.get("name") or tags.get("ref")
        elif element.get("type") == "relation":
            for member in element.get("members") or []:
                if member.get("type") == "way" and (coords := _line_coords(member.get("geometry") or [])):
                    lines.append(coords)
            name = tags.get("name") or tags.get("ref") or name
            kind = "route"
    if not lines:
        return None

    thinned = [_sample(line, _MAX_POINTS_PER_LINE) for line in lines]
    geometry: dict[str, Any] = (
        {"type": "LineString", "coordinates": _to_lnglat(thinned[0])}
        if len(thinned) == 1
        else {"type": "MultiLineString", "coordinates": [_to_lnglat(line) for line in thinned]}
    )
    return {"name": name or "Trail (OSM)", "kind": kind, "geometry": geometry}


def resolve_trail_network(
    con: psycopg.Connection, trailhead_id: str, *, client: httpx.Client | None = None
) -> scoring.TrailPath | None:
    """The real trail for a selected trailhead, falling back to the nearest cached path/route.

    Returns None only if the trailhead id itself doesn't resolve to a cached trailhead row -
    callers (``api.py``) treat that as a 404, distinct from "no trail found" (which still returns
    a ``TrailPath`` when the nearest-cached fallback succeeds).
    """
    trailhead = scoring.get_trail(con, trailhead_id)
    if trailhead is None or trailhead.kind != "trailhead":
        return None
    node_id = _parse_trailhead_id(trailhead_id)
    live = trailhead_network(node_id, client=client)
    if live is not None:
        trail = dataclasses.replace(trailhead, name=live["name"], kind=live["kind"], geometry=live["geometry"])
        return scoring.TrailPath(trail=trail, authoritative=True)
    nearest = scoring.nearest_trail(con, lat=trailhead.center_lat, lng=trailhead.center_lng)
    if nearest is None:
        return None
    return scoring.TrailPath(trail=nearest, authoritative=False)


def fetch_trails_bbox(
    *,
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    timeout_s: int = 300,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
    raise_on_error: bool = False,
) -> list[tuple[Any, ...]]:
    """Fetch OSM paths, hiking routes, and trailheads within a bbox (state-sized) as trails rows.

    Best-effort by default (``raise_on_error=False``, matching ``fetch_trails``): a failing or
    malformed Overpass response is logged and yields ``[]`` rather than aborting the refresh.
    ``ingest_trails_region`` passes ``raise_on_error=True`` instead, since it needs to tell a
    tile that's genuinely empty apart from one that failed, to decide whether the region is
    safe to mark as fully ingested.
    """
    owns = client is None
    client = client or httpx.Client(timeout=timeout_s + 30.0)
    try:
        if progress_cb:
            progress_cb("Fetching trails…", 50.0)
        payload = _post_overpass(client, _trails_query_bbox(min_lat, min_lng, max_lat, max_lng, timeout_s=timeout_s))
        rows = _parse_trails(payload)
        logger.info("trails: %d trails/routes/trailheads", len(rows))
        return rows
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        if raise_on_error:
            raise
        logger.warning("trails: bbox query failed (%s) - skipping", error)
        return []
    finally:
        if owns:
            client.close()


def ingest_trails_region(
    region: CoverageRegion,
    con: psycopg.Connection | None = None,
    *,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest the OSM trail network for one coverage region (state) into ``trails``.

    Unlike land ownership, Overpass can't handle a whole-US query in one request, so full US
    coverage means looping this per region - see ``cli.py``'s ``refresh --with trails --all``.
    Within a region, the bbox is further tiled (see ``_tile_bboxes``) and each tile's rows are
    upserted immediately rather than accumulated - a trail-dense state's full response can be
    large enough to OOM a small droplet before it's even parsed. The returned/logged count is
    rows upserted, not distinct trails - a route spanning a tile boundary gets upserted (and
    counted) once per tile it touches, though it's the same row each time (id is the primary
    key). One-shot per region: skips once ``trails:place:{place_id}`` is in ``ingest_log``.
    """
    if region.bbox is None:
        raise ValueError(f"{region.name} has no bbox configured for trails ingest")
    own_con = con is None
    database = con if con is not None else connect()
    key = f"trails:place:{region.place_id}"
    if is_ingested(database, key):
        logger.info("trails: %s already ingested, skipping", region.name)
        if progress_cb:
            progress_cb(f"Trails already cached for {region.name}, skipping…", 100.0)
        if own_con:
            database.close()
        return 0
    try:
        west, south, east, north = region.bbox
        tiles = _tile_bboxes(south, west, north, east)
        logger.info("trails: fetching OSM trail network for %s (%d tiles)…", region.name, len(tiles))
        total = 0
        had_failures = False
        for index, (tile_south, tile_west, tile_north, tile_east) in enumerate(tiles, start=1):
            if progress_cb:
                progress_cb(f"Fetching trails for {region.name} ({index}/{len(tiles)})…", (index / len(tiles)) * 100.0)
            try:
                rows = fetch_trails_bbox(
                    min_lat=tile_south,
                    min_lng=tile_west,
                    max_lat=tile_north,
                    max_lng=tile_east,
                    client=client,
                    raise_on_error=True,
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                logger.warning(
                    "trails: tile %d/%d for %s failed (%s) - region won't be marked ingested, will retry next run",
                    index,
                    len(tiles),
                    region.name,
                    error,
                )
                had_failures = True
                continue
            upsert_trails(database, rows)
            total += len(rows)
        # Only mark the region done if every tile succeeded - a partial failure still leaves
        # its successful tiles' rows cached (upserted above), but a future run needs to retry
        # the whole region rather than believing it's fully covered.
        if had_failures:
            logger.warning("trails: %s only partially ingested (%d rows) - not recording as done", region.name, total)
        else:
            record_ingest(database, key, total)
        logger.info("trails: cached %d trails in %s", total, region.name)
        return total
    finally:
        if own_con:
            database.close()
