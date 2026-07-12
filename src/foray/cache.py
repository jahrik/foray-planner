"""DuckDB cache: schema, idempotent upserts, and the ingest log.

Observations are keyed by iNat id, so re-ingesting the same window is a no-op
(``ON CONFLICT DO NOTHING``). Region binning (grid cell) is derived in SQL from
lat/lng and ``cell_deg`` so it is never stored redundantly.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import duckdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS taxa (
    taxon_id     BIGINT PRIMARY KEY,
    name         VARCHAR,
    common_name  VARCHAR,
    rank         VARCHAR
);

CREATE TABLE IF NOT EXISTS observations (
    id                  BIGINT PRIMARY KEY,
    taxon_id            BIGINT,
    lat                 DOUBLE,
    lng                 DOUBLE,
    observed_on         DATE,
    month               SMALLINT,
    year                SMALLINT,
    quality_grade       VARCHAR,
    positional_accuracy INTEGER
);

CREATE TABLE IF NOT EXISTS ingest_log (
    key           VARCHAR PRIMARY KEY,   -- e.g. "obs:47348:47.6:-122.3:150:2015-01-01:2026-07-11"
    fetched_at    TIMESTAMP,
    row_count     BIGINT
);

-- Campsites: developed campgrounds (Recreation.gov RIDB) plus the OSM dispersed-camping layer
-- (reported sites + a road∩public-land proxy). Keyed by "{source}:{source_id}" so re-ingesting
-- the same area is a no-op. `free` is nullable on purpose: we only assert free when the source
-- says so (or, for the dispersed proxy, because public-land camping carries no fee), never guess.
CREATE TABLE IF NOT EXISTS campsites (
    id          VARCHAR PRIMARY KEY,   -- "{source}:{source_id}", e.g. "ridb:250018", "osm:way/42"
    name        VARCHAR,
    kind        VARCHAR,               -- "campground" (RIDB), "reported"/"dispersed" (OSM)
    fee         VARCHAR,               -- raw fee description when known, else NULL
    free        BOOLEAN,               -- TRUE on an explicit no-fee signal (RIDB/OSM tag) OR for
                                       --   the dispersed proxy (public-land camping is free of
                                       --   charge by nature, not from a per-site tag); else NULL
    lat         DOUBLE,
    lng         DOUBLE,
    source      VARCHAR,               -- "ridb", "osm"
    url         VARCHAR
);

-- Public-land ownership polygons (BLM Surface Management Agency + USFS admin forest
-- boundaries, via ArcGIS REST). Keyed by "{source}:{source_id}" so re-ingesting the same
-- area is a no-op. Geometry is stored as GeoJSON *text* and the bounding box as plain
-- columns, so the read/map path needs no DuckDB spatial extension — a cheap bbox filter
-- serves the "land near here" query. Informational only: this shows ownership and links the
-- official source; it never asserts camping legality (see AGENTS.md).
CREATE TABLE IF NOT EXISTS public_land (
    id          VARCHAR PRIMARY KEY,   -- "{source}:{source_id}", e.g. "usfs:1234"
    agency      VARCHAR,               -- "BLM", "USFS"
    unit        VARCHAR,               -- unit / forest name when the source provides one
    source      VARCHAR,               -- "blm", "usfs"
    url         VARCHAR,               -- official source (the ArcGIS service)
    min_lat     DOUBLE,                -- geometry bounding box, for radius filtering
    min_lng     DOUBLE,
    max_lat     DOUBLE,
    max_lng     DOUBLE,
    geojson     VARCHAR                -- polygon geometry as GeoJSON text
);

-- Trails (OSM Overpass): hiking paths/footways, named hiking routes, and trailheads. Keyed by
-- "{source}:{osm_type}/{osm_id}" so re-ingesting the same area is a no-op. Geometry is stored as
-- GeoJSON *text* (LineString/MultiLineString for paths/routes, Point for trailheads) with a
-- bounding box + a representative center point, so the read/map path needs no spatial extension:
-- a cheap bbox filter serves "trails near here", and haversine on the center ranks by distance.
-- Informational only: links the OSM source; makes no legal-access claim (see AGENTS.md).
CREATE TABLE IF NOT EXISTS trails (
    id          VARCHAR PRIMARY KEY,   -- "{source}:{osm_type}/{osm_id}", e.g. "osm:way/42"
    name        VARCHAR,
    kind        VARCHAR,               -- "path" (way) | "route" (relation) | "trailhead" (node)
    source      VARCHAR,               -- "osm"
    url         VARCHAR,               -- official source (the OSM element page)
    min_lat     DOUBLE,                -- geometry bounding box, for radius filtering
    min_lng     DOUBLE,
    max_lat     DOUBLE,
    max_lng     DOUBLE,
    center_lat  DOUBLE,                -- representative point on the trail, for distance ranking
    center_lng  DOUBLE,
    geojson     VARCHAR                -- GeoJSON text (LineString / MultiLineString / Point)
);
"""


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    return con


def upsert_taxa(con: duckdb.DuckDBPyConnection, rows: Iterable[dict[str, Any]]) -> None:
    con.executemany(
        """
        INSERT INTO taxa (taxon_id, name, common_name, rank)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (taxon_id) DO UPDATE SET
            name = excluded.name,
            common_name = excluded.common_name,
            rank = excluded.rank
        """,
        [(row["taxon_id"], row["name"], row["common_name"], row["rank"]) for row in rows],
    )


def upsert_observations(con: duckdb.DuckDBPyConnection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Insert observation tuples, ignoring ones already present. Returns rows attempted."""
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO observations
            (id, taxon_id, lat, lng, observed_on, month, year, quality_grade, positional_accuracy)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO NOTHING
        """,
        rows,
    )
    return len(rows)


def upsert_campsites(con: duckdb.DuckDBPyConnection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert campsite tuples, refreshing existing rows in place. Returns rows attempted.

    Each tuple is (id, name, kind, fee, free, lat, lng, source, url).
    """
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO campsites (id, name, kind, fee, free, lat, lng, source, url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            name = excluded.name,
            kind = excluded.kind,
            fee = excluded.fee,
            free = excluded.free,
            lat = excluded.lat,
            lng = excluded.lng,
            source = excluded.source,
            url = excluded.url
        """,
        rows,
    )
    return len(rows)


def upsert_public_land(con: duckdb.DuckDBPyConnection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert public-land polygons, refreshing existing rows in place. Returns rows attempted.

    Each tuple is (id, agency, unit, source, url, min_lat, min_lng, max_lat, max_lng, geojson).
    """
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO public_land
            (id, agency, unit, source, url, min_lat, min_lng, max_lat, max_lng, geojson)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            agency = excluded.agency,
            unit = excluded.unit,
            source = excluded.source,
            url = excluded.url,
            min_lat = excluded.min_lat,
            min_lng = excluded.min_lng,
            max_lat = excluded.max_lat,
            max_lng = excluded.max_lng,
            geojson = excluded.geojson
        """,
        rows,
    )
    return len(rows)


def upsert_trails(con: duckdb.DuckDBPyConnection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert trail tuples, refreshing existing rows in place. Returns rows attempted.

    Each tuple is
    (id, name, kind, source, url, min_lat, min_lng, max_lat, max_lng, center_lat, center_lng,
    geojson).
    """
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO trails
            (id, name, kind, source, url, min_lat, min_lng, max_lat, max_lng,
             center_lat, center_lng, geojson)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            name = excluded.name,
            kind = excluded.kind,
            source = excluded.source,
            url = excluded.url,
            min_lat = excluded.min_lat,
            min_lng = excluded.min_lng,
            max_lat = excluded.max_lat,
            max_lng = excluded.max_lng,
            center_lat = excluded.center_lat,
            center_lng = excluded.center_lng,
            geojson = excluded.geojson
        """,
        rows,
    )
    return len(rows)


def record_ingest(con: duckdb.DuckDBPyConnection, key: str, row_count: int) -> None:
    con.execute(
        """
        INSERT INTO ingest_log (key, fetched_at, row_count)
        VALUES (?, now(), ?)
        ON CONFLICT (key) DO UPDATE SET fetched_at = now(), row_count = excluded.row_count
        """,
        [key, row_count],
    )


def observation_count(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("SELECT count(*) FROM observations").fetchone()
    return int(row[0]) if row else 0
