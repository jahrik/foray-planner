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

-- Campsites: developed campgrounds (Recreation.gov RIDB) and, later, dispersed-camping
-- zones. Keyed by "{source}:{source_id}" so re-ingesting the same area is a no-op.
-- `free` is nullable on purpose: we only assert free when the source says so, never guess.
CREATE TABLE IF NOT EXISTS campsites (
    id          VARCHAR PRIMARY KEY,   -- "{source}:{source_id}", e.g. "ridb:250018"
    name        VARCHAR,
    kind        VARCHAR,               -- e.g. "campground", "dispersed" (later)
    fee         VARCHAR,               -- raw fee description when known, else NULL
    free        BOOLEAN,               -- TRUE only on an explicit no-fee signal, else NULL
    lat         DOUBLE,
    lng         DOUBLE,
    source      VARCHAR,               -- "ridb", ...
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
