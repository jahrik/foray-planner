"""Postgres cache: schema, idempotent upserts, and the ingest log.

Observations are keyed by iNat id, so re-ingesting the same window is a no-op
(``ON CONFLICT DO NOTHING``). Region binning (grid cell) is derived in SQL from
lat/lng and ``cell_deg`` so it is never stored redundantly.

Connections are opened with ``autocommit=True`` (matching the DuckDB-era code's implicit
per-statement commit semantics, which nothing here was written against explicit
transactions for) - callers that need atomicity across statements (e.g.
``scoring.build_phenology``'s drop+rebuild) wrap them in an explicit
``with con.transaction():`` block instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import psycopg

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS taxa (
    taxon_id     BIGINT PRIMARY KEY,
    name         TEXT,
    common_name  TEXT,
    rank         TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    id                  BIGINT PRIMARY KEY,
    taxon_id            BIGINT,
    lat                 DOUBLE PRECISION,
    lng                 DOUBLE PRECISION,
    observed_on         DATE,
    month               SMALLINT,
    year                SMALLINT,
    quality_grade       TEXT,
    positional_accuracy INTEGER
);

CREATE TABLE IF NOT EXISTS ingest_log (
    key           TEXT PRIMARY KEY,   -- e.g. "obs:47348:47.6:-122.3:150:2015-01-01:2026-07-11"
    fetched_at    TIMESTAMP,
    row_count     BIGINT
);

-- Campsites: developed campgrounds (Recreation.gov RIDB) plus the OSM dispersed-camping layer
-- (reported sites + a road∩public-land proxy). Keyed by "{source}:{source_id}" so re-ingesting
-- the same area is a no-op. `free` is nullable on purpose: we only assert free when the source
-- says so (or, for the dispersed proxy, because public-land camping carries no fee), never guess.
CREATE TABLE IF NOT EXISTS campsites (
    id          TEXT PRIMARY KEY,    -- "{source}:{source_id}", e.g. "ridb:250018", "osm:way/42"
    name        TEXT,
    kind        TEXT,                -- "campground" (RIDB), "reported"/"dispersed" (OSM)
    fee         TEXT,                -- raw fee description when known, else NULL
    free        BOOLEAN,             -- TRUE on an explicit no-fee signal (RIDB/OSM tag) OR for
                                      --   the dispersed proxy (public-land camping is free of
                                      --   charge by nature, not from a per-site tag); else NULL
    lat         DOUBLE PRECISION,
    lng         DOUBLE PRECISION,
    source      TEXT,                -- "ridb", "osm"
    url         TEXT
);

-- Public-land ownership polygons (BLM Surface Management Agency + USFS admin forest
-- boundaries, via ArcGIS REST). Keyed by "{source}:{source_id}" so re-ingesting the same
-- area is a no-op. Geometry is stored as GeoJSON *text* and the bounding box as plain
-- columns, so the read/map path needs no PostGIS geometry types - a cheap bbox filter
-- serves the "land near here" query. Informational only: this shows ownership and links the
-- official source; it never asserts camping legality (see AGENTS.md).
CREATE TABLE IF NOT EXISTS public_land (
    id          TEXT PRIMARY KEY,    -- "{source}:{source_id}", e.g. "usfs:1234"
    agency      TEXT,                -- "BLM", "USFS"
    unit        TEXT,                -- unit / forest name when the source provides one
    source      TEXT,                -- "blm", "usfs"
    url         TEXT,                -- official source (the ArcGIS service)
    min_lat     DOUBLE PRECISION,    -- geometry bounding box, for radius filtering
    min_lng     DOUBLE PRECISION,
    max_lat     DOUBLE PRECISION,
    max_lng     DOUBLE PRECISION,
    geojson     TEXT                 -- polygon geometry as GeoJSON text
);

-- Trails (OSM Overpass): hiking paths, named hiking routes, and trailheads. Keyed by
-- "{source}:{osm_type}/{osm_id}" so re-ingesting the same area is a no-op. Geometry is stored as
-- GeoJSON *text* (LineString/MultiLineString for paths/routes, Point for trailheads) with a
-- bounding box + a representative center point, so the read/map path needs no PostGIS geometry
-- types: a cheap bbox filter serves "trails near here", and haversine on the center ranks by
-- distance. Informational only: links the OSM source; makes no legal-access claim (see AGENTS.md).
CREATE TABLE IF NOT EXISTS trails (
    id          TEXT PRIMARY KEY,    -- "{source}:{osm_type}/{osm_id}", e.g. "osm:way/42"
    name        TEXT,
    kind        TEXT,                -- "path" (way) | "route" (relation) | "trailhead" (node)
    source      TEXT,                -- "osm"
    url         TEXT,                -- official source (the OSM element page)
    min_lat     DOUBLE PRECISION,    -- geometry bounding box, for radius filtering
    min_lng     DOUBLE PRECISION,
    max_lat     DOUBLE PRECISION,
    max_lng     DOUBLE PRECISION,
    center_lat  DOUBLE PRECISION,    -- representative point on the trail, for distance ranking
    center_lng  DOUBLE PRECISION,
    geojson     TEXT                 -- GeoJSON text (LineString / MultiLineString / Point)
);

-- Single-row settings table: the UI's "Set location" override, which used to live in a
-- location.json sidecar file next to the DuckDB cache. Fargate's container storage is
-- ephemeral, so this now has to be durable state in Postgres instead. The BOOLEAN PK +
-- CHECK enforces at most one row.
CREATE TABLE IF NOT EXISTS app_location (
    id        BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    name      TEXT NOT NULL,
    lat       DOUBLE PRECISION NOT NULL,
    lng       DOUBLE PRECISION NOT NULL,
    radius_km DOUBLE PRECISION NOT NULL
);
"""


def connect(conninfo: str = "") -> psycopg.Connection:
    """Open a Postgres connection and ensure the schema exists.

    ``conninfo`` empty (the default) means "use libpq's usual env vars"
    (``PGHOST``/``PGPORT``/``PGUSER``/``PGPASSWORD``/``PGDATABASE``), which is how the
    ECS task and local dev (via ``docker-compose.yml``'s port mapping + a ``.env``) are
    wired - no DSN-building code needed in the app. Tests pass an explicit DSN/conninfo
    string instead.
    """
    con = psycopg.connect(conninfo, autocommit=True)
    con.execute(SCHEMA)
    return con


def upsert_taxa(con: psycopg.Connection, rows: Iterable[dict[str, Any]]) -> None:
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO taxa (taxon_id, name, common_name, rank)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (taxon_id) DO UPDATE SET
                name = EXCLUDED.name,
                common_name = EXCLUDED.common_name,
                rank = EXCLUDED.rank
            """,
            [(row["taxon_id"], row["name"], row["common_name"], row["rank"]) for row in rows],
        )


def upsert_observations(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Insert observation tuples, ignoring ones already present. Returns rows attempted."""
    if not rows:
        return 0
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO observations
                (id, taxon_id, lat, lng, observed_on, month, year, quality_grade,
                 positional_accuracy)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            rows,
        )
    return len(rows)


def upsert_campsites(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert campsite tuples, refreshing existing rows in place. Returns rows attempted.

    Each tuple is (id, name, kind, fee, free, lat, lng, source, url).
    """
    if not rows:
        return 0
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO campsites (id, name, kind, fee, free, lat, lng, source, url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                kind = EXCLUDED.kind,
                fee = EXCLUDED.fee,
                free = EXCLUDED.free,
                lat = EXCLUDED.lat,
                lng = EXCLUDED.lng,
                source = EXCLUDED.source,
                url = EXCLUDED.url
            """,
            rows,
        )
    return len(rows)


def upsert_public_land(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert public-land polygons, refreshing existing rows in place. Returns rows attempted.

    Each tuple is (id, agency, unit, source, url, min_lat, min_lng, max_lat, max_lng, geojson).
    """
    if not rows:
        return 0
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO public_land
                (id, agency, unit, source, url, min_lat, min_lng, max_lat, max_lng, geojson)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                agency = EXCLUDED.agency,
                unit = EXCLUDED.unit,
                source = EXCLUDED.source,
                url = EXCLUDED.url,
                min_lat = EXCLUDED.min_lat,
                min_lng = EXCLUDED.min_lng,
                max_lat = EXCLUDED.max_lat,
                max_lng = EXCLUDED.max_lng,
                geojson = EXCLUDED.geojson
            """,
            rows,
        )
    return len(rows)


def upsert_trails(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert trail tuples, refreshing existing rows in place. Returns rows attempted.

    Each tuple is
    (id, name, kind, source, url, min_lat, min_lng, max_lat, max_lng, center_lat, center_lng,
    geojson).
    """
    if not rows:
        return 0
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO trails
                (id, name, kind, source, url, min_lat, min_lng, max_lat, max_lng,
                 center_lat, center_lng, geojson)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                kind = EXCLUDED.kind,
                source = EXCLUDED.source,
                url = EXCLUDED.url,
                min_lat = EXCLUDED.min_lat,
                min_lng = EXCLUDED.min_lng,
                max_lat = EXCLUDED.max_lat,
                max_lng = EXCLUDED.max_lng,
                center_lat = EXCLUDED.center_lat,
                center_lng = EXCLUDED.center_lng,
                geojson = EXCLUDED.geojson
            """,
            rows,
        )
    return len(rows)


def record_ingest(con: psycopg.Connection, key: str, row_count: int) -> None:
    con.execute(
        """
        INSERT INTO ingest_log (key, fetched_at, row_count)
        VALUES (%s, now(), %s)
        ON CONFLICT (key) DO UPDATE SET fetched_at = now(), row_count = EXCLUDED.row_count
        """,
        [key, row_count],
    )


def observation_count(con: psycopg.Connection) -> int:
    row = con.execute("SELECT count(*) FROM observations").fetchone()
    return int(row[0]) if row else 0


def is_ingested(con: psycopg.Connection, key: str) -> bool:
    row = con.execute("SELECT 1 FROM ingest_log WHERE key = %s", [key]).fetchone()
    return row is not None


def latest_obs_date(
    con: psycopg.Connection, taxon_id: int, lat: float, lng: float, radius_km: float
) -> str | None:
    prefix = f"obs:{taxon_id}:{lat}:{lng}:{radius_km}:%"
    rows = con.execute("SELECT key FROM ingest_log WHERE key LIKE %s", [prefix]).fetchall()
    if not rows:
        return None
    dates = [row[0].split(":")[-1] for row in rows]
    return max(dates)


def load_location(con: psycopg.Connection) -> dict[str, Any] | None:
    """The UI's "Set location" override, if one has been saved. `None` = use config.yaml's."""
    row = con.execute(
        "SELECT name, lat, lng, radius_km FROM app_location WHERE id = true"
    ).fetchone()
    if row is None:
        return None
    name, lat, lng, radius_km = row
    return {"name": name, "lat": lat, "lng": lng, "radius_km": radius_km}


def save_location(
    con: psycopg.Connection, *, name: str, lat: float, lng: float, radius_km: float
) -> None:
    con.execute(
        """
        INSERT INTO app_location (id, name, lat, lng, radius_km)
        VALUES (true, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            lat = EXCLUDED.lat,
            lng = EXCLUDED.lng,
            radius_km = EXCLUDED.radius_km
        """,
        [name, lat, lng, radius_km],
    )
