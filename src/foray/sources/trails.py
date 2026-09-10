"""Trail layer from OpenStreetMap (Overpass API).

The planner's question is "shortest walk from where I can park to where they're fruiting", so this
module pulls the walkable network near home from OSM and caches it as ``trails`` rows the map and
scoring read directly. One ODbL-licensed Overpass request gathers four kinds of feature (the
way/node union and the route relations need separate ``out geom`` statements - see
``_trails_query``):

* **Paths** (``kind='path'``) - backcountry/trail ways (``highway=path`` / ``bridleway``), cached
  as a ``LineString`` polyline. We deliberately *exclude* ``highway=footway``: it is dominated by
  urban sidewalks (measured ~6x the row count over a wide radius - e.g. 44.7k vs 7.4k ways at
  200 km), which is noise for a mushroom-trail planner and heavy enough to time the full-radius
  query out on public Overpass.
* **Forest roads** (``kind='road'``) - old logging / forest-service roads (``highway=track``, and
  ``highway=service`` with ``service=forestry``), cached as a ``LineString``. These are a primary
  mushroom-foraging surface - you drive or walk them through habitat - so they are ingested as a
  first-class, separately filterable kind, not folded into paths.
* **Hiking routes** (``kind='route'``) - named long trails (``route=hiking`` relations), cached as
  a ``MultiLineString`` stitched from their member ways.
* **Trailheads** (``kind='trailhead'``) - where you actually start walking (``highway=trailhead``
  nodes), cached as a ``Point``.

Geometry is stored as GeoJSON *text* plus a representative center point (see ``cache.trails``);
the ``geom`` GIST index (PostGIS Phase 1, issue #268) serves "trails near here" and the exact
point-to-trail distance. Each way's vertices are thinned to keep the cached polylines light
enough for a phone map.

Like the campground, land, and dispersed ingests, this is best-effort: a failing Overpass request
is logged and skipped rather than aborting the whole refresh. It is informational only - it links
the OSM source and makes no legal-access claim (see AGENTS.md, "No claims").

Scale note: even ``highway=path`` alone grows with radius (~7.4k ways at 200 km around Coos Bay,
~15k at the full 400 km), but stays inside Overpass's server budget as a single query. If a future
radius pushes past that, tile the home disk into sub-queries the way ``camps.py`` does - the read
path (the ``geom`` GIST index) is unaffected either way.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from itertools import pairwise
from typing import Any

import httpx
import psycopg

from foray import scoring
from foray.cache import connection, forget_ingest, is_ingested, record_ingest, upsert_trails
from foray.config import CoverageRegion, Settings
from foray.geo import haversine_km
from foray.sources import overpass
from foray.sources.http import SOURCE_ERRORS
from foray.sources.ingest_base import run_area_ingest

logger = logging.getLogger(__name__)

# A single path can carry hundreds of vertices; the map only needs enough to trace its shape, so
# each line is thinned to at most this many evenly-spaced points before caching.
_MAX_POINTS_PER_LINE = 60

# A whole state's Overpass response (with full geometry for every path/route/trailhead) can be
# large enough to OOM a small droplet before it's even parsed - a well-mapped state like Arizona
# killed a 1 GB box mid-response. Tiling each region into degree-sized sub-queries bounds a
# single response's size regardless of how big or trail-dense the region is; each tile's rows
# are upserted and discarded before the next tile starts; see ``ingest_trails_region``.
_TILE_DEG = 2.0

# Bump when the Overpass query in `_way_selectors` / `_trails_query_bbox` changes what it pulls.
# `ingest_trails_region` keys its one-shot `ingest_log` marker on this, so a region ingested
# under an older query no longer matches and the weekly `refresh --with trails --all` cron
# re-pulls it automatically - no manual re-ingest, same idea as `cache._MIGRATIONS`.
#   1 - highway=path + trailheads + route=hiking relations
#   2 - adds highway=track / service=forestry (kind='road') and highway=bridleway
#   3 - adds barrier=gate/bollard/... nodes, matched onto road ways as a synthetic barrier attr
_TRAILS_QUERY_VERSION = 3

# Way classes we ingest, by the ``kind`` they become. Trails are foot/horse ways; roads are the
# old logging / forest-service roads foragers actually walk and drive (issue: forest roads are a
# primary foraging surface). Everything else - paved public roads, and ``footway`` (~6x the row
# count, mostly urban sidewalks) - is the vector basemap's job to draw, not ours to score on.
_TRAIL_HIGHWAYS = ("path", "bridleway")
_ROAD_HIGHWAYS = ("track",)
_ROAD_HIGHWAY_SET = frozenset(_ROAD_HIGHWAYS)


def _is_road(tags: dict[str, Any]) -> bool:
    """A forest / logging road (``highway=track``, or ``highway=service`` + ``service=forestry``)
    rather than a foot/horse trail - cached as ``kind='road'`` so it stays separately queryable
    from trails without a second table."""
    highway = tags.get("highway")
    return highway in _ROAD_HIGHWAY_SET or (highway == "service" and tags.get("service") == "forestry")


def _way_selectors(region_filter: str) -> str:
    """The ``way[...]`` clauses (trails + forest roads) for a region filter fragment - shared by
    the home-disk and bbox queries so both ingest the same classes."""
    trail_alt = "|".join(_TRAIL_HIGHWAYS)
    road_alt = "|".join(_ROAD_HIGHWAYS)
    return (
        f'way["highway"~"^({trail_alt})$"]{region_filter};'
        f'way["highway"~"^({road_alt})$"]{region_filter};'
        f'way["highway"="service"]["service"="forestry"]{region_filter};'
    )


# Barrier nodes that stop a vehicle but let a walker through - a gate on a forest road is a
# stronger walk-in signal than the (often absent) access tags. Matched to a way at ingest by
# coordinate; see ``_parse_trails``. ``cattle_grid`` is deliberately out - vehicles drive over it.
_BARRIER_NODES = ("gate", "lift_gate", "swing_gate", "bollard", "block", "chain")


def _barrier_selector(region_filter: str) -> str:
    alt = "|".join(_BARRIER_NODES)
    return f'node["barrier"~"^({alt})$"]{region_filter};'


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


def _trails_query(lat: float, lng: float, radius_m: float) -> str:
    """Overpass QL for trails, forest roads, hiking routes, and trailheads within the home disk."""
    region = f"({overpass.around(lat, lng, radius_m)})"
    return (
        "[out:json][timeout:180];"
        "("
        f"{_way_selectors(region)}"
        f'node["highway"="trailhead"]{region};'
        f"{_barrier_selector(region)}"
        ");"
        "out geom tags;"
        # Relations need their own `out geom`: inside a union `out geom` a route relation comes
        # back with only `bounds` and no members, so `_parse_element` can never stitch it and
        # `kind='route'` rows silently never appear (issue #306). A dedicated statement returns
        # every member way with its geometry.
        f'relation["route"="hiking"]{region};'
        "out geom;"
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
        ".segs out geom tags;"
        # Its own `out geom` for the relation - a shared one drops members (issue #306).
        'rel(bw.segs)["route"="hiking"];'
        "out geom;"
    )


def _trails_query_bbox(min_lat: float, min_lng: float, max_lat: float, max_lng: float, *, timeout_s: int = 300) -> str:
    """Overpass QL for the same element classes as ``_trails_query`` within a state-sized bbox.

    A whole state (rather than a home-radius circle) is large enough that the query needs a
    longer server-side timeout - Overpass rejects a query outright if its own [timeout:N] is
    exceeded, so this defaults higher than the home-radius query's 180s.
    """
    region = overpass.bbox(min_lat, min_lng, max_lat, max_lng)
    return (
        f"[out:json][timeout:{timeout_s}];"
        "("
        f"{_way_selectors(region)}"
        f'node["highway"="trailhead"]{region};'
        f"{_barrier_selector(region)}"
        ");"
        "out geom tags;"
        # See `_trails_query`: a union `out geom` drops relation members, so route relations
        # get their own statement (issue #306).
        f'relation["route"="hiking"]{region};'
        "out geom;"
    )


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


def _to_lnglat(coords: Sequence[tuple[float, float]]) -> list[list[float]]:
    """(lat, lng) tuples -> GeoJSON [lng, lat] pairs (GeoJSON is x=lng, y=lat)."""
    return [[lng, lat] for lat, lng in coords]


def _trail_url(etype: str, eid: int) -> str:
    return f"https://www.openstreetmap.org/{etype}/{eid}"


# Detail tags kept per path/road/route row. `highway`/`tracktype`/`surface`/`smoothness`/
# `4wd_only` describe what you're walking or driving; `access`/`motor_vehicle` whether a forest
# road is gated (walk-in - prime foraging, and a positive term in the road relevance sort - see
# `scoring.queries._walk_in`); `ref` the road number (FR 300) even when `name` is set. The card
# renders these ("FR 300 - dirt - drivable" vs "Ridge Trail - footpath").
_ATTR_TAGS = (
    "highway",
    "surface",
    "tracktype",
    "smoothness",
    "4wd_only",
    "sac_scale",
    "trail_visibility",
    "network",
    "operator",
    "informal",
    "access",
    "motor_vehicle",
    "foot",
    "ref",
)


def _attrs(tags: dict[str, Any]) -> dict[str, str] | None:
    """The OSM detail tags worth keeping for a path/road/route row, or None if it carries none."""
    picked = {tag: str(tags[tag]) for tag in _ATTR_TAGS if tags.get(tag)}
    return picked or None


def _polyline_length_km(lines: Sequence[Sequence[tuple[float, float]]]) -> float | None:
    """Great-circle length of the (multi-)polyline over its *full* vertex list, or None for a
    lone point. Computed before ``_sample`` thins the geometry so a long trail keeps its real
    length."""
    total = sum(haversine_km(a[0], a[1], b[0], b[1]) for line in lines for a, b in pairwise(line))
    return round(total, 3) if total > 0 else None


def _row(
    etype: str,
    eid: int,
    name: str,
    kind: str,
    lines: Sequence[Sequence[tuple[float, float]]],
    connects: list[str] | None = None,
    attrs: dict[str, str] | None = None,
) -> tuple[Any, ...] | None:
    """Build a trails row from one or more (lat, lng) polylines, or None if all are empty.

    A lone vertex (a trailhead node) becomes a ``Point``; a single line a ``LineString``; several
    a ``MultiLineString``. The center is the middle vertex of the concatenated geometry so it
    lands on the trail, not in its bbox gap. ``connects`` (trailhead rows only) is the trail ids
    whose geometry passes within ``_LINK_SNAP_M`` of the node - see ``_link_trailheads``.
    ``attrs`` (path/route rows) is the kept OSM detail tags; ``length_km`` is derived here.
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
    center_lat, center_lng = flat[len(flat) // 2]
    return (
        f"osm:{etype}/{eid}",
        name,
        kind,
        "osm",
        _trail_url(etype, eid),
        center_lat,
        center_lng,
        json.dumps(geometry, separators=(",", ":")),
        connects,
        _polyline_length_km(lines),
        json.dumps(attrs, separators=(",", ":")) if attrs else None,
    )


_BARRIER_NODE_SET = frozenset(_BARRIER_NODES)


def _gate_points(payload: dict[str, Any]) -> frozenset[tuple[float, float]]:
    """Rounded (lat, lng) of every vehicle-stopping barrier node in the payload.

    Overpass returns a way's vertex coordinates and a standalone node's own coordinates from the
    same OSM node identically, so a gate that sits on a forest road is matched to that road by an
    exact (rounded) coordinate hit in ``_parse_element``."""
    return frozenset(
        (round(float(el["lat"]), 6), round(float(el["lon"]), 6))
        for el in payload.get("elements", [])
        if el.get("type") == "node"
        and (el.get("tags") or {}).get("barrier") in _BARRIER_NODE_SET
        and el.get("lat") is not None
        and el.get("lon") is not None
    )


def _parse_element(
    element: dict[str, Any], *, gate_points: frozenset[tuple[float, float]] = frozenset()
) -> tuple[Any, ...] | None:
    """One Overpass element -> a trails row tuple, or None if it carries no usable geometry.

    ``gate_points`` (from ``_gate_points``): a road way with one of these on its line gets a
    synthetic ``barrier=gate`` attr so ``scoring.queries._walk_in`` reads it as walk-in."""
    etype = element.get("type")
    eid = element.get("id")
    if eid is None:
        return None
    tags = element.get("tags") or {}
    if etype == "node":
        # The query also returns barrier=gate nodes (consumed by ``_gate_points``); only a
        # highway=trailhead node becomes a row.
        if tags.get("highway") != "trailhead":
            return None
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
        kind = "road" if _is_road(tags) else "path"
        fallback = "Forest road (OSM)" if kind == "road" else "Trail (OSM)"
        name = tags.get("name") or tags.get("ref") or fallback
        attrs = _attrs(tags)
        if kind == "road" and gate_points and any((round(la, 6), round(ln, 6)) in gate_points for la, ln in coords):
            attrs = {**(attrs or {}), "barrier": "gate"}
        return _row("way", int(eid), name, kind, [coords], attrs=attrs)
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
        return _row("relation", int(eid), name, "route", lines, attrs=_attrs(tags))
    return None


_LINK_SNAP_M = 35.0  # a trailhead node this close to a trail's polyline is treated as connected
_LINK_CELL_DEG = 0.02  # ~2 km grid cells for the trailhead/trail spatial prefilter


def _point_polyline_m(point: tuple[float, float], coords: Sequence[tuple[float, float]]) -> float:
    """Metres from ``point`` to the nearest point on the ``coords`` polyline (equirectangular -
    fine at the tens-of-metres scale ``_LINK_SNAP_M`` cares about)."""
    m_per_deg_lat = 111_320.0
    m_per_deg_lng = 111_320.0 * math.cos(math.radians(point[0]))
    px, py = point[1] * m_per_deg_lng, point[0] * m_per_deg_lat
    if len(coords) == 1:
        return math.hypot(px - coords[0][1] * m_per_deg_lng, py - coords[0][0] * m_per_deg_lat)
    best = math.inf
    for (a_lat, a_lng), (b_lat, b_lng) in pairwise(coords):
        ax, ay = a_lng * m_per_deg_lng, a_lat * m_per_deg_lat
        dx, dy = b_lng * m_per_deg_lng - ax, b_lat * m_per_deg_lat - ay
        seg_sq = dx * dx + dy * dy
        t = 0.0 if seg_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_sq))
        best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best


def _trail_polylines(payload: dict[str, Any]) -> list[tuple[str, list[tuple[float, float]]]]:
    """(trail row id, (lat, lng) polyline) for every path/route way in the payload - the
    geometry a trailhead node can snap onto. A route contributes one entry per member way."""
    lines: list[tuple[str, list[tuple[float, float]]]] = []
    for element in payload.get("elements", []):
        etype, eid = element.get("type"), element.get("id")
        if eid is None:
            continue
        if etype == "way" and (coords := _line_coords(element.get("geometry") or [])):
            lines.append((f"osm:way/{eid}", coords))
        elif etype == "relation":
            lines.extend(
                (f"osm:relation/{eid}", coords)
                for member in element.get("members") or []
                if member.get("type") == "way" and (coords := _line_coords(member.get("geometry") or []))
            )
    return lines


def _cell(lat: float, lng: float) -> tuple[int, int]:
    """Grid cell for a coordinate. ``math.floor`` (not ``int``, which truncates toward zero and
    would double-width the cell straddling the equator / prime meridian) keeps cells uniform for
    negative longitudes - i.e. everywhere in the US."""
    return math.floor(lat / _LINK_CELL_DEG), math.floor(lng / _LINK_CELL_DEG)


def _group_key(tags: dict[str, Any]) -> str | None:
    """The label the OSM way segments of one real trail or road share, so a trailhead touching
    one segment links to the whole feature (issue #306).

    A named way groups by ``name``. An unnamed forest road groups by its ``ref`` + ``operator``:
    OSM splits a long ``FR 300`` into dozens of separate ways, each unnamed but each carrying
    ``ref=FR 300`` - without this a trailhead touching one segment would link to just that
    ~0.5 km piece instead of the whole road. This only widens the ``connects`` expansion in
    ``_link_trailheads``; the cache still stores one row per OSM way. Returns None for a way
    that carries neither, which stays an ungrouped single segment.
    """
    name = tags.get("name")
    if name:
        return str(name)
    ref = tags.get("ref")
    if ref and _is_road(tags):
        return f"ref:{ref}\x1f{tags.get('operator') or ''}"
    return None


def _link_trailheads(payload: dict[str, Any]) -> dict[str, list[str]]:
    """Trailhead row id -> ids of the trails whose geometry passes within ``_LINK_SNAP_M``.

    Computed here, at ingest, from geometry already in the payload so ``resolve_trail_network``
    can draw a selected trailhead's trail straight from the cache instead of a live Overpass
    query (issue #306). A coarse vertex grid keeps it to a handful of distance checks per node.
    """
    lines = _trail_polylines(payload)
    grid: dict[tuple[int, int], set[int]] = defaultdict(set)
    for index, (_id, coords) in enumerate(lines):
        for lat, lng in coords:
            grid[_cell(lat, lng)].add(index)

    # OSM splits one real trail or forest road into many ways; group them (by name, or by
    # ref+operator for an unnamed forest road - see `_group_key`) so a trailhead that touches one
    # segment links to the whole "Wonderland Trail" / whole "FR 300", not the 200 m stub by the
    # parking lot (issue #306). Route relations already come as one row and are left alone.
    kin: dict[str, set[str]] = defaultdict(set)
    id_group: dict[str, str] = {}
    for element in payload.get("elements", []):
        if element.get("type") == "way" and element.get("id") is not None:
            group = _group_key(element.get("tags") or {})
            if group:
                way_id = f"osm:way/{element['id']}"
                kin[group].add(way_id)
                id_group[way_id] = group

    links: dict[str, list[str]] = {}
    for element in payload.get("elements", []):
        if element.get("type") != "node" or (element.get("tags") or {}).get("highway") != "trailhead":
            continue
        lat, lng = element.get("lat"), element.get("lon")
        if lat is None or lng is None:
            continue
        node = (float(lat), float(lng))
        cell_lat, cell_lng = _cell(*node)
        candidates: set[int] = set()
        for d_lat in (-1, 0, 1):
            for d_lng in (-1, 0, 1):
                candidates |= grid.get((cell_lat + d_lat, cell_lng + d_lng), set())
        hits = {lines[index][0] for index in candidates if _point_polyline_m(node, lines[index][1]) <= _LINK_SNAP_M}
        expanded = set(hits)
        for hit in hits:
            expanded |= kin.get(id_group.get(hit, ""), set())
        if expanded:
            links[f"osm:node/{element['id']}"] = sorted(expanded)
    return links


def _parse_trails(payload: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Overpass payload -> trails rows, deduped by id (paths, hiking routes, trailheads).

    Trailhead rows are annotated with ``connects`` - the ids of the trails their node touches
    (``_link_trailheads``) - so a selection draws from cache without a live query.
    """
    links = _link_trailheads(payload)
    gate_points = _gate_points(payload)
    by_id: dict[str, tuple[Any, ...]] = {}
    for element in payload.get("elements", []):
        row = _parse_element(element, gate_points=gate_points)
        if row is None:
            continue
        row_id: str = row[0]
        if row[2] == "trailhead" and (connects := links.get(row_id)):
            row = (*row[:8], connects, *row[9:])
        by_id[row_id] = row
    rows = list(by_id.values())
    counts = Counter(row[2] for row in rows)
    logger.info(
        "trails: parsed %d rows (%d path, %d road, %d route, %d trailhead; %d trailheads linked)",
        len(rows),
        counts["path"],
        counts["road"],
        counts["route"],
        counts["trailhead"],
        len(links),
    )
    return rows


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
        payload = overpass.post(client, _trails_query(lat, lng, radius_m))
        rows = _parse_trails(payload)
        return rows
    except SOURCE_ERRORS as error:
        logger.warning("trails: query failed (%s) - skipping", error)
        return []
    finally:
        if owns:
            client.close()


def ingest_trails(
    cfg: Settings,
    con: psycopg.Connection | None = None,
    *,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest the OSM trail network near home into ``trails``. Returns rows upserted."""
    return run_area_ingest(
        cfg,
        con,
        prefix="trails:",
        label="trails",
        noun="Trails",
        fetch=lambda **kw: fetch_trails(client=client, **kw),
        upsert=upsert_trails,
        progress_cb=progress_cb,
    )


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
        payload = overpass.post(client, _network_query(node_id))
    except SOURCE_ERRORS as error:
        logger.warning("trails: network query failed for node %d (%s) - falling back", node_id, error)
        return None
    finally:
        if owns:
            client.close()

    lines: list[list[tuple[float, float]]] = []
    rows: list[tuple[Any, ...]] = []
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
            lines.extend(
                coords
                for member in element.get("members") or []
                if member.get("type") == "way" and (coords := _line_coords(member.get("geometry") or []))
            )
            name = tags.get("name") or tags.get("ref") or name
            kind = "route"
        # A proper cache row for each way/route, so resolve_trail_network can persist the link
        # (issue #306): the next selection of this trailhead reads from cache, not Overpass.
        if element.get("type") in ("way", "relation") and (row := _parse_element(element)) is not None:
            rows.append(row)
    if not lines:
        return None

    thinned = [_sample(line, _MAX_POINTS_PER_LINE) for line in lines]
    geometry: dict[str, Any] = (
        {"type": "LineString", "coordinates": _to_lnglat(thinned[0])}
        if len(thinned) == 1
        else {"type": "MultiLineString", "coordinates": [_to_lnglat(line) for line in thinned]}
    )
    return {"name": name or "Trail (OSM)", "kind": kind, "geometry": geometry, "rows": rows}


_SYNTHETIC_NAMES = {"Trail (OSM)", "Forest road (OSM)", "Hiking route (OSM)", "Trailhead (OSM)"}


def _merge_connected(trailhead: scoring.Trail, parts: Sequence[scoring.Trail]) -> scoring.Trail | None:
    """One ``Trail`` covering every connected path/route, or None if none carry geometry.

    Coordinates are concatenated into a single LineString/MultiLineString; the name is the best
    real name among the parts (a named route wins over a named path), falling back to the
    trailhead's own name. ``kind`` is ``route`` if any part is a route, else ``path``.
    """
    lines: list[list[list[float]]] = []
    for part in parts:
        geometry = part.geometry or {}
        if geometry.get("type") == "LineString":
            lines.append(geometry["coordinates"])
        elif geometry.get("type") == "MultiLineString":
            lines.extend(geometry["coordinates"])
    if not lines:
        return None
    routes = [p for p in parts if p.kind == "route" and p.name not in _SYNTHETIC_NAMES]
    named = [p for p in parts if p.name not in _SYNTHETIC_NAMES]
    name = (routes or named or [trailhead])[0].name
    kind = "route" if any(p.kind == "route" for p in parts) else "path"
    geometry = (
        {"type": "LineString", "coordinates": lines[0]}
        if len(lines) == 1
        else {"type": "MultiLineString", "coordinates": lines}
    )
    return dataclasses.replace(trailhead, name=name, kind=kind, geometry=geometry)


def resolve_trail_network(
    con: psycopg.Connection, trailhead_id: str, *, client: httpx.Client | None = None
) -> scoring.TrailPath | None:
    """The real trail for a row selected in a destination card's Trails tab.

    For a ``path``/``route``/``road`` id (the card lists these too - issue #306 C1): that row's
    own geometry, stitched with its same-name sibling segments, always authoritative.

    For a ``trailhead`` node id, order of preference: the ingest-time cached link
    (``trails.connects``, issue #306 - no network), then a live Overpass topology query, then
    the nearest cached path/route as a proximity guess (``authoritative=False``).

    Raises ``LookupError`` if the id isn't a cached trail at all (``api.py`` treats that as a
    404); returns None if the row is known but no geometry could be found for it.
    """
    trail = scoring.get_trail(con, trailhead_id)
    if trail is None:
        raise LookupError(f"no trail cached for id {trailhead_id!r}")

    # A path / route / road id selected straight from the card list (issue #306 C2): draw that
    # feature's own geometry, stitched with its same-name sibling segments (OSM stores one row
    # per way). Always authoritative and never needs a live query - the card only listed it
    # because it's already cached.
    if trail.kind != "trailhead":
        named = bool(trail.name) and trail.name not in _SYNTHETIC_NAMES
        segments = (
            scoring.trail_segments_by_name(con, name=trail.name, kind=trail.kind, ref_id=trail.id) if named else [trail]
        )
        merged = _merge_connected(trail, segments) or trail
        if merged.geometry is None:  # no cached geometry at all - a real 404, per the contract below
            return None
        return scoring.TrailPath(trail=merged, authoritative=True)

    trailhead = trail
    if trailhead.connects:
        merged = _merge_connected(trailhead, scoring.connected_trails(con, trailhead.connects))
        if merged is not None:
            return scoring.TrailPath(trail=merged, authoritative=True)

    node_id = _parse_trailhead_id(trailhead_id)
    live = trailhead_network(node_id, client=client)
    if live is not None:
        _persist_resolved_link(con, trailhead, node_id, live["rows"])
        trail = dataclasses.replace(trailhead, name=live["name"], kind=live["kind"], geometry=live["geometry"])
        return scoring.TrailPath(trail=trail, authoritative=True)
    nearest = scoring.nearest_trail(con, lat=trailhead.center_lat, lng=trailhead.center_lng)
    if nearest is None:
        return None
    return scoring.TrailPath(trail=nearest, authoritative=False)


def _persist_resolved_link(
    con: psycopg.Connection, trailhead: scoring.Trail, node_id: int, trail_rows: Sequence[tuple[Any, ...]]
) -> None:
    """Write a live-resolved trailhead->trail link into the cache so the next selection is
    instant (issue #306). Best-effort: a DB hiccup here must not fail the selection, which
    already has its geometry."""
    if not trail_rows:
        return
    try:
        upsert_trails(con, trail_rows)
        connects = sorted({row[0] for row in trail_rows})
        point = (trailhead.center_lat, trailhead.center_lng)
        row = _row("node", node_id, trailhead.name, "trailhead", [[point]], connects=connects)
        if row is not None:
            upsert_trails(con, [row])
        con.commit()
        logger.info("trails: cached resolved link for %s -> %d trail(s)", trailhead.id, len(connects))
    except psycopg.Error as error:
        logger.warning("trails: could not persist resolved link for %s (%s)", trailhead.id, error)
        con.rollback()


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
        payload = overpass.post(client, _trails_query_bbox(min_lat, min_lng, max_lat, max_lng, timeout_s=timeout_s))
        rows = _parse_trails(payload)
        return rows
    except SOURCE_ERRORS as error:
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
    force: bool = False,
) -> int:
    """Ingest the OSM trail network for one coverage region (state) into ``trails``.

    Unlike land ownership, Overpass can't handle a whole-US query in one request, so full US
    coverage means looping this per region - see ``cli.py``'s ``refresh --with trails --all``.
    Within a region, the bbox is further tiled (see ``_tile_bboxes``) and each tile's rows are
    upserted immediately rather than accumulated - a trail-dense state's full response can be
    large enough to OOM a small droplet before it's even parsed. The returned/logged count is
    rows upserted, not distinct trails - a route spanning a tile boundary gets upserted (and
    counted) once per tile it touches, though it's the same row each time (id is the primary
    key).

    One-shot per region: the ``ingest_log`` marker is ``trails:place:{place_id}:q{version}``
    where ``version`` is ``_TRAILS_QUERY_VERSION``. Widening the Overpass query bumps that
    constant, so every region's marker stops matching and the next ``refresh --with trails
    --all`` cron re-pulls it - no manual step. ``force`` additionally re-pulls without a version
    bump (OSM data drift, debugging one region); superseded-version markers are pruned on a
    successful run.
    """
    if region.bbox is None:
        raise ValueError(f"{region.name} has no bbox configured for trails ingest")
    key = f"trails:place:{region.place_id}:q{_TRAILS_QUERY_VERSION}"
    with connection(con) as database:
        if force and forget_ingest(database, key):
            logger.info("trails: --force cleared the ingest marker for %s, re-fetching", region.name)
        if not force and is_ingested(database, key):
            logger.info("trails: %s already ingested at query v%d, skipping", region.name, _TRAILS_QUERY_VERSION)
            if progress_cb:
                progress_cb(f"Trails already cached for {region.name}, skipping…", 100.0)
            return 0
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
            except SOURCE_ERRORS as error:
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
            # Drop this region's markers from older query versions (and the pre-versioning
            # `trails:place:{id}` key) so ingest_log doesn't accrete a stale row per bump.
            database.execute(
                "DELETE FROM ingest_log WHERE (key LIKE %s OR key = %s) AND key <> %s",
                [f"trails:place:{region.place_id}:q%", f"trails:place:{region.place_id}", key],
            )
        logger.info("trails: cached %d trails in %s", total, region.name)
        return total


# Observations within this of a trail line feed its ``forage_obs`` count - the genus-agnostic
# "how much fruits along here" signal the map ramps and the card shows. Matches the query-time
# relevance radius (``scoring.queries._OBS_RELEVANCE_RADIUS_M``); keep the two in step.
_FORAGE_OBS_RADIUS_M = 500
# One backfill pass re-counts at most this many trails (oldest ``forage_obs_at`` first, NULLs
# ahead of them). ~3s per 5k rows locally against a full ~1.2M-row trails table, so the default
# pass is ~15s; a frequent cron then cycles the whole table over a couple of weeks. Bounded on
# purpose - prod PG is a single vCPU and this one UPDATE holds row locks for its duration. The
# operator can raise it via ``--limit`` / ``FORAY_FORAGE_LIMIT``.
_FORAGE_BACKFILL_BATCH = 25000

_FORAGE_BACKFILL_SQL = """
    WITH batch AS (
        SELECT id FROM trails
        WHERE geom IS NOT NULL
        ORDER BY forage_obs_at NULLS FIRST, id
        LIMIT %s
    )
    UPDATE trails t SET
        forage_obs = (
            SELECT count(*) FROM observations o
            WHERE o.geom IS NOT NULL
              AND o.quality_grade = 'research'
              AND NOT COALESCE(o.obscured, false)
              AND ST_DWithin(o.geom, t.geom, %s)
        ),
        forage_obs_at = now()
    FROM batch
    WHERE t.id = batch.id
"""


def backfill_forage_obs(con: psycopg.Connection | None = None, *, max_trails: int | None = None) -> int:
    """Refresh ``trails.forage_obs`` - the count of research-grade, non-obscured fungi
    observations within ``_FORAGE_OBS_RADIUS_M`` of each trail line.

    Processes the ``max_trails`` (default ``_FORAGE_BACKFILL_BATCH``) rows whose count is
    oldest, NULLs first, so a frequent small cron cycles the whole table and keeps it roughly
    fresh as observations drift. Returns the number of rows updated. Purely set-based - no
    external calls - so it can't fail an ingest it's wired after; it just does one bounded pass.
    """
    limit = max_trails if max_trails is not None else _FORAGE_BACKFILL_BATCH
    with connection(con) as database:
        updated = database.execute(_FORAGE_BACKFILL_SQL, [limit, _FORAGE_OBS_RADIUS_M]).rowcount
    if updated:
        logger.info("trails: refreshed forage_obs for %d trails", updated)
    return updated
