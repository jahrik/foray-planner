"""OSM trails + forest roads as a bulk snapshot from Geofabrik extracts (issue #442).

The same OSM elements ``trails.py`` fetches live from Overpass - foot/horse paths
(``highway=path|bridleway``), forest roads (``highway=track``, ``service=forestry``), hiking
route relations, trailhead nodes and vehicle-stopping barrier nodes - but read from Geofabrik's
daily per-state ``.osm.pbf`` extracts in GitHub Actions instead of crawled region by region
from a shared public API on the droplet. A full Overpass pull took days, failed on timeouts, and
(before #441) OOM-killed the 2 GB droplet; this stages the whole country in minutes of CI time,
and the weekly re-stage is what keeps OSM edits flowing in (the Overpass path was one-shot per
query version).

Rows come out of ``trails._parse_trails`` unchanged: each state's extract is converted into the
Overpass-shaped element list that function already takes, so name fallbacks, gate detection,
route-member skipping and trailhead linking are identical whichever path produced a row - and
identical rows are what lets the diff load (``trails_snapshot.load_snapshot``) skip everything
that didn't change.

Per state, so runner disk and memory stay bounded by the largest state (California, ~1.3 GB)
rather than the 11.6 GB national file. Hiking routes are the one thing assembled across states:
a route relation's member ways are collected from every extract they appear in and the route row
is built once at the end, so a route crossing state lines (the PCT) is whole, not just the last
state's piece. Geofabrik extracts keep every way that crosses a state line whole, so a way seen
in two extracts produces the same row twice; the first one wins.

Overpass stays for what it fits: the home-radius ingest and live trailhead resolution.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import osmium
import osmium.filter
import osmium.osm
import psycopg

from foray.config import Settings
from foray.sources import trails, trails_snapshot
from foray.sources.http import USER_AGENT

logger = logging.getLogger(__name__)

_BULK_SOURCE = "osm_trails"
_GEOFABRIK_BASE = "https://download.geofabrik.de/north-america/us"
_DOWNLOAD_CHUNK = 1024 * 1024


def _state_slug(name: str) -> str:
    """Coverage region name -> Geofabrik extract slug ("New York" -> "new-york")."""
    return name.lower().replace(" ", "-")


def _wanted_way(tags: osmium.osm.TagList) -> bool:
    """The way classes ``trails._way_selectors`` asks Overpass for - trails, plus forest roads by
    ``trails._is_road``'s own test."""
    return tags.get("highway") in trails._TRAIL_HIGHWAYS or trails._is_road(tags)


def _download(client: httpx.Client, url: str, dest: Path) -> None:
    """Stream ``url`` to ``dest`` and check it against Geofabrik's published ``.md5``."""
    digest = hashlib.md5(usedforsecurity=False)
    with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as resp:
        resp.raise_for_status()
        with dest.open("wb") as out:
            for chunk in resp.iter_bytes(_DOWNLOAD_CHUNK):
                out.write(chunk)
                digest.update(chunk)
    expected_resp = client.get(f"{url}.md5", headers={"User-Agent": USER_AGENT})
    expected_resp.raise_for_status()
    expected = expected_resp.text.split()[0]
    if digest.hexdigest() != expected:
        raise RuntimeError(f"osm_trails: {url} failed its md5 check (got {digest.hexdigest()}, expected {expected})")


class _Routes:
    """Hiking route relations accumulated across every state's extract."""

    def __init__(self) -> None:
        self.tags: dict[int, dict[str, str]] = {}
        self.members: dict[int, list[int]] = {}
        self.geometry: dict[int, list[tuple[float, float]]] = {}

    def element(self, relation_id: int) -> dict[str, Any]:
        """An Overpass-shaped relation element from whatever member geometry has been seen."""
        return {
            "type": "relation",
            "id": relation_id,
            "tags": self.tags[relation_id],
            "members": [
                {"type": "way", "ref": way_id, "role": "", "geometry": self.geometry[way_id]}
                for way_id in self.members[relation_id]
                if way_id in self.geometry
            ],
        }


def _state_elements(path: Path, routes: _Routes) -> list[dict[str, Any]]:
    """One extract -> the Overpass-shaped element list ``trails._parse_trails`` takes.

    Two passes. The first reads only relations, to learn which ways are hiking-route members
    (a member way needn't carry any tag we'd otherwise keep). The second caches every node's
    location and lets libosmium drop everything without a ``highway`` / ``barrier`` key before
    Python sees it - node locations are stored before filters run, so filtered-out untagged
    nodes still give the kept ways their coordinates."""
    state_relations: list[int] = []
    for relation in osmium.FileProcessor(str(path), osmium.osm.RELATION).with_filter(
        osmium.filter.TagFilter(("route", "hiking"))
    ):
        if not isinstance(relation, osmium.osm.Relation):
            continue
        state_relations.append(relation.id)
        routes.tags[relation.id] = dict(relation.tags)
        members = routes.members.setdefault(relation.id, [])
        for member in relation.members:
            if member.type == "w" and member.ref not in members:
                members.append(member.ref)
    member_ways = {way_id for relation_id in state_relations for way_id in routes.members[relation_id]}

    elements: list[dict[str, Any]] = []
    processor = (
        osmium.FileProcessor(str(path), osmium.osm.NODE | osmium.osm.WAY)
        .with_locations()
        .with_filter(osmium.filter.KeyFilter("highway", "barrier"))
    )
    for obj in processor:
        if isinstance(obj, osmium.osm.Node):
            if obj.tags.get("highway") == "trailhead" or obj.tags.get("barrier") in trails._BARRIER_NODES:
                location = obj.location
                elements.append(
                    {"type": "node", "id": obj.id, "lat": location.lat, "lon": location.lon, "tags": dict(obj.tags)}
                )
            continue
        if not isinstance(obj, osmium.osm.Way):
            continue
        wanted = _wanted_way(obj.tags)
        if not wanted and obj.id not in member_ways:
            continue
        geometry = [(node.lat, node.lon) for node in obj.nodes if node.location.valid()]
        if wanted:
            elements.append({"type": "way", "id": obj.id, "tags": dict(obj.tags), "geometry": geometry})
        if obj.id in member_ways:
            routes.geometry[obj.id] = geometry
    elements.extend(routes.element(relation_id) for relation_id in state_relations)
    return elements


def _iter_rows(cfg: Settings, client: httpx.Client, workdir: Path) -> Iterator[tuple[Any, ...]]:
    """Every covered state's rows, then the cross-state hiking routes. Raises (so nothing is
    published) if a state fails to download or yields nothing."""
    routes = _Routes()
    seen: set[str] = set()
    for region in cfg.coverage:
        slug = _state_slug(region.name)
        path = workdir / f"{slug}.osm.pbf"
        logger.info("osm_trails: downloading %s", slug)
        _download(client, f"{_GEOFABRIK_BASE}/{slug}-latest.osm.pbf", path)
        try:
            rows = trails._parse_trails({"elements": _state_elements(path, routes)})
        finally:
            path.unlink(missing_ok=True)
        if not rows:
            raise RuntimeError(f"osm_trails: {slug} produced no trails - refusing to stage a partial snapshot")
        for row in rows:
            # Routes are emitted once, below, from every state's member geometry.
            if row[2] == "route" or row[0] in seen:
                continue
            seen.add(row[0])
            yield row
    for relation_id in routes.tags:
        row = trails._parse_element(routes.element(relation_id))
        if row is not None:
            yield row


def stage_osm_trails(cfg: Settings, snapshot_date: date, run_id: str, *, client: httpx.Client | None = None) -> None:
    """Stager: build the national OSM trails snapshot from Geofabrik's per-state extracts and
    upload it under this run's Space prefix. Runs in GitHub Actions (no DB). Any failure
    propagates - ``ingest_bulk.stage_snapshot`` publishes right after this returns, and a
    partial snapshot would read as authoritative to the loader's prune step."""
    owns = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(60.0, read=300.0), follow_redirects=True)
    try:
        with tempfile.TemporaryDirectory(prefix="osm-trails-") as workdir:
            count = trails_snapshot.write_snapshot(
                cfg, _BULK_SOURCE, snapshot_date, run_id, _iter_rows(cfg, client, Path(workdir))
            )
    finally:
        if owns:
            client.close()
    logger.info("osm_trails: staged %d OSM trails, forest roads, routes and trailheads", count)


def load_osm_trails(con: psycopg.Connection, cfg: Settings, snapshot_date: date, run_id: str) -> None:
    """Loader: diff-load the newest staged OSM snapshot into ``trails`` (``source='osm'``)."""
    trails_snapshot.load_snapshot(
        con, cfg, bulk_source=_BULK_SOURCE, trails_source="osm", snapshot_date=snapshot_date, run_id=run_id
    )
