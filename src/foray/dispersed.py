"""Dispersed-camping layer from OpenStreetMap (Overpass API).

Two ODbL-licensed signals, both cached as ``campsites`` rows so they flow through the existing
``camps_near`` scoring and ``/api/camps`` plumbing untouched:

* **Reported campsites** (``kind='reported'``) — places actually tagged in OSM as somewhere to
  camp (``tourism=camp_site`` / ``camp_pitch``, ``backcountry=yes``). Real spots, but coverage is
  only wherever a mapper has added one.
* **Dispersed-legal proxy** (``kind='dispersed'``) — there is *no* authoritative dataset of legal
  dispersed sites, so we approximate: a drivable track (``highway=track`` / ``unclassified``)
  whose geometry falls inside cached BLM/USFS ``public_land`` becomes a candidate "likely
  dispersed-legal" point. This is the piece that uses the DuckDB **spatial** extension
  (point-in-polygon), and only on the *ingest* (write) path — the served rows are plain lat/lng,
  so the field/offline read path never loads spatial.

Neither signal is a promise: dispersed camping is labelled *likely* legal and always links the
OSM source — verify with the managing district (see AGENTS.md, "No claims"). ``free`` is TRUE on
the proxy points because dispersed camping on public land is free of charge; the *legality*
caveat rides on ``kind`` and the UI label, not on the cost flag.

Commercial camping apps (iOverlander, The Dyrt) are deliberately *not* used: iOverlander's terms
license its content for personal, non-commercial use only (no redistribution/storage), and The
Dyrt exposes no open API — so OSM is the only source we can legally cache and re-serve.

Like the campground and land ingests, a failing Overpass request is skipped rather than aborting
the whole refresh; the reported-sites and track queries are issued separately so a heavy-track
timeout still leaves the reported sites ingested.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import duckdb
import httpx

from foray.cache import connect, record_ingest, upsert_campsites
from foray.config import Config

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "foray-planner/0.1 (mushroom trip planner; +https://github.com/jahrik)"

# Overpass is a shared free service; pace the two queries and back off on its throttle/timeout
# codes (429 / 504) rather than hammering it.
_MIN_REQUEST_INTERVAL = 1.0
# A single track can carry hundreds of nodes; we only need enough to tell whether it enters
# public land, so each way is thinned to at most this many evenly-spaced points before the
# (relatively expensive) point-in-polygon join.
_MAX_POINTS_PER_WAY = 25


@dataclass(frozen=True)
class Road:
    """One drivable OSM way: its id, optional name/ref, and (thinned) (lat, lng) vertices."""

    way_id: int
    name: str | None
    coords: tuple[tuple[float, float], ...]


def _around(lat: float, lng: float, radius_m: float) -> str:
    return f"around:{radius_m:.0f},{lat},{lng}"


def _reported_query(lat: float, lng: float, radius_m: float) -> str:
    """Overpass QL for OSM-tagged campable places within the home disk."""
    around = _around(lat, lng, radius_m)
    return (
        "[out:json][timeout:120];"
        "("
        f'nwr["tourism"="camp_site"]({around});'
        f'nwr["tourism"="camp_pitch"]({around});'
        f'nwr["backcountry"="yes"]({around});'
        ");"
        "out center tags;"
    )


def _tracks_query(lat: float, lng: float, radius_m: float) -> str:
    """Overpass QL for drivable tracks (the dispersed proxy's road layer)."""
    around = _around(lat, lng, radius_m)
    return (
        f'[out:json][timeout:180];way["highway"~"^(track|unclassified)$"]({around});out tags geom;'
    )


def _post_overpass(
    client: httpx.Client, query: str, *, attempts: int = 4, base_delay: float = 2.0
) -> dict[str, Any]:
    """POST a query, backing off on Overpass's throttle (429) / timeout (504) responses."""
    resp: httpx.Response | None = None
    for attempt in range(1, attempts + 1):
        resp = client.post(OVERPASS_URL, data={"data": query}, headers={"User-Agent": USER_AGENT})
        if resp.status_code in (429, 504) and attempt < attempts:
            retry_after = resp.headers.get("Retry-After", "")
            delay = (
                float(retry_after)
                if retry_after.replace(".", "", 1).isdigit()
                else base_delay * 2 ** (attempt - 1)
            )
            time.sleep(delay)
            continue
        break
    assert resp is not None
    resp.raise_for_status()
    return resp.json()


def _element_point(element: dict[str, Any]) -> tuple[float, float] | None:
    """(lat, lng) of an Overpass element — its own coords (node) or its `center` (way/rel)."""
    if element.get("lat") is not None and element.get("lon") is not None:
        return float(element["lat"]), float(element["lon"])
    center = element.get("center") or {}
    if center.get("lat") is not None and center.get("lon") is not None:
        return float(center["lat"]), float(center["lon"])
    return None


def _reported_name(tags: dict[str, Any]) -> str:
    """A descriptive fallback name when an OSM campable place has no `name` tag."""
    if tags.get("backcountry") == "yes":
        return "Backcountry campsite (OSM)"
    if tags.get("tourism") == "camp_pitch":
        return "Camp pitch (OSM)"
    return "Campsite (OSM)"


def _parse_reported(payload: dict[str, Any]) -> list[tuple[Any, ...]]:
    """Overpass payload -> campsites rows for OSM-tagged campable places (kind='reported')."""
    rows: list[tuple[Any, ...]] = []
    for element in payload.get("elements", []):
        etype = element.get("type")
        eid = element.get("id")
        if etype not in ("node", "way", "relation") or eid is None:
            continue
        point = _element_point(element)
        if point is None:
            continue
        lat, lng = point
        tags = element.get("tags") or {}
        fee_tag = str(tags.get("fee") or "").strip().lower()
        # Only assert free on an explicit no-fee tag; a fee/condition tag is surfaced as a cost
        # note but never as a dollar figure we don't have; silence stays unknown (see AGENTS.md).
        if fee_tag == "no":
            free, fee = True, None
        elif fee_tag:
            free, fee = None, "fee required"
        else:
            free, fee = None, None
        rows.append(
            (
                f"osm:{etype}/{eid}",
                tags.get("name") or _reported_name(tags),
                "reported",
                fee,
                free,
                lat,
                lng,
                "osm",
                f"https://www.openstreetmap.org/{etype}/{eid}",
            )
        )
    return rows


def _sample(coords: Sequence[tuple[float, float]], count: int) -> list[tuple[float, float]]:
    """Thin a vertex list to at most ``count`` evenly-spaced points, keeping first and last."""
    if len(coords) <= count:
        return list(coords)
    step = (len(coords) - 1) / (count - 1)
    return [coords[round(index * step)] for index in range(count)]


def _parse_tracks(payload: dict[str, Any]) -> list[Road]:
    """Overpass payload -> drivable Roads (ways with inline `geometry`), vertices thinned."""
    roads: list[Road] = []
    for element in payload.get("elements", []):
        if element.get("type") != "way":
            continue
        way_id = element.get("id")
        geometry = element.get("geometry") or []
        coords = [
            (float(node["lat"]), float(node["lon"]))
            for node in geometry
            if node.get("lat") is not None and node.get("lon") is not None
        ]
        if way_id is None or not coords:
            continue
        tags = element.get("tags") or {}
        roads.append(
            Road(
                way_id=int(way_id),
                name=tags.get("name") or tags.get("ref"),
                coords=tuple(_sample(coords, _MAX_POINTS_PER_WAY)),
            )
        )
    return roads


def fetch_dispersed_sources(
    *,
    lat: float,
    lng: float,
    radius_km: float,
    client: httpx.Client | None = None,
    min_interval: float = _MIN_REQUEST_INTERVAL,
    progress_cb: Callable[[str, float], None] | None = None,
) -> tuple[list[tuple[Any, ...]], list[Road]]:
    """Fetch OSM reported campsites + drivable tracks near home. Each query is best-effort.

    Returns ``(reported_rows, roads)``. The two Overpass queries are independent so a heavy-track
    timeout still yields the reported sites — a failing query is logged and skipped, never fatal.
    """
    owns = client is None
    client = client or httpx.Client(timeout=180.0)
    radius_m = radius_km * 1000.0
    reported: list[tuple[Any, ...]] = []
    roads: list[Road] = []
    try:
        try:
            if progress_cb:
                progress_cb("Fetching reported campsites…", 0.0)
            payload = _post_overpass(client, _reported_query(lat, lng, radius_m))
            reported = _parse_reported(payload)
            logger.info("dispersed: %d reported OSM campsites", len(reported))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            logger.warning("dispersed: reported-sites query failed (%s) — skipping", error)
        if min_interval > 0:
            time.sleep(min_interval)
        try:
            if progress_cb:
                progress_cb("Fetching drivable tracks…", 50.0)
            payload = _post_overpass(client, _tracks_query(lat, lng, radius_m))
            roads = _parse_tracks(payload)
            logger.info("dispersed: %d drivable tracks", len(roads))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            logger.warning("dispersed: tracks query failed (%s) — skipping", error)
    finally:
        if owns:
            client.close()
    return reported, roads


def dispersed_proxy_rows(
    con: duckdb.DuckDBPyConnection, roads: Sequence[Road]
) -> list[tuple[Any, ...]]:
    """Tracks whose geometry falls inside cached public land -> 'likely dispersed-legal' points.

    One representative point per way (its first vertex inside any BLM/USFS polygon) becomes a
    ``kind='dispersed'`` campsite. Uses the DuckDB spatial extension for the point-in-polygon
    test — this is the only place it's needed, and only at ingest time. Yields ``[]`` when there
    are no roads or no cached public land to intersect against.
    """
    if not roads:
        return []
    land_count = con.execute("SELECT count(*) FROM public_land").fetchone()
    if not land_count or not land_count[0]:
        logger.info("dispersed: no public_land cached — skipping road proxy")
        return []

    try:
        con.execute("LOAD spatial")
    except duckdb.Error:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
    # ArcGIS polygons are server-generalized and can be slightly self-intersecting; ST_MakeValid
    # keeps a bad ring from aborting the whole join.
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _land_geom AS
        SELECT ST_MakeValid(ST_GeomFromGeoJSON(geojson)) AS geom,
               min_lat, min_lng, max_lat, max_lng
        FROM public_land
        """
    )
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _road_pts "
        "(way_id BIGINT, seq INTEGER, name VARCHAR, lat DOUBLE, lng DOUBLE)"
    )
    point_rows = [
        (road.way_id, seq, road.name, lat, lng)
        for road in roads
        for seq, (lat, lng) in enumerate(road.coords)
    ]
    con.executemany("INSERT INTO _road_pts VALUES (?, ?, ?, ?, ?)", point_rows)
    # Cheap bbox pre-filter gates the expensive ST_Contains; keep one point per way (lowest seq
    # inside any polygon). ST_Point takes (x=lng, y=lat).
    inside = con.execute(
        """
        WITH hits AS (
            SELECT p.way_id, p.name, p.lat, p.lng, p.seq,
                   row_number() OVER (PARTITION BY p.way_id ORDER BY p.seq) AS rn
            FROM _road_pts p
            JOIN _land_geom l
              ON p.lat BETWEEN l.min_lat AND l.max_lat
             AND p.lng BETWEEN l.min_lng AND l.max_lng
             AND ST_Contains(l.geom, ST_Point(p.lng, p.lat))
        )
        SELECT way_id, name, lat, lng FROM hits WHERE rn = 1
        """
    ).fetchall()
    con.execute("DROP TABLE IF EXISTS _road_pts")
    con.execute("DROP TABLE IF EXISTS _land_geom")
    return [
        (
            f"osm:way/{way_id}",
            name or "Drivable track on public land",
            "dispersed",
            None,  # fee: dispersed camping carries no per-site fee
            True,  # free: free of charge on public land (legality caveat rides on kind + label)
            lat,
            lng,
            "osm",
            f"https://www.openstreetmap.org/way/{way_id}",
        )
        for way_id, name, lat, lng in inside
    ]


def ingest_dispersed(
    cfg: Config,
    con: duckdb.DuckDBPyConnection | None = None,
    *,
    client: httpx.Client | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> int:
    """Ingest OSM reported campsites + the road∩public-land proxy into ``campsites``.

    Returns rows upserted. The road proxy is best-effort on top of the fetch: if the spatial
    extension can't load (e.g. offline first run) the reported sites still ingest.
    """
    own_con = con is None
    database = con if con is not None else connect(cfg.db_path)
    home = cfg.home
    try:
        logger.info(
            "dispersed: fetching OSM camping layers within %.0f km of home…", home.radius_km
        )
        reported, roads = fetch_dispersed_sources(
            lat=home.lat,
            lng=home.lng,
            radius_km=home.radius_km,
            client=client,
            progress_cb=progress_cb,
        )
        try:
            proxy = dispersed_proxy_rows(database, roads)
            logger.info("dispersed: %d road∩public-land proxy zones", len(proxy))
        except (duckdb.Error, OSError, ValueError) as error:
            logger.warning("dispersed: road proxy skipped (%s)", error)
            proxy = []
        rows = reported + proxy
        upsert_campsites(database, rows)
        key = f"dispersed:{home.lat}:{home.lng}:{home.radius_km}"
        record_ingest(database, key, len(rows))
        logger.info("dispersed: cached %d dispersed/reported sites", len(rows))
        return len(rows)
    finally:
        if own_con:
            database.close()
