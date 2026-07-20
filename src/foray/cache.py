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

import logging
from collections.abc import Iterable, Sequence
from typing import Any

import psycopg

from foray.scoring import haversine_km

logger = logging.getLogger(__name__)

# Split from SCHEMA (rather than its first statement) because a managed Postgres app role
# (e.g. on RDS, if it isn't the master/rds_superuser account) may lack CREATE EXTENSION
# privilege - failing this alone shouldn't take the whole schema bootstrap down with it, nor
# surface as an opaque low-level DB error. PostGIS is only needed by the dispersed-camping
# ingest's point-in-polygon proxy (see dispersed.py), which already degrades gracefully
# (best-effort, catches psycopg.Error) when it's unavailable.
_ENABLE_POSTGIS = "CREATE EXTENSION IF NOT EXISTS postgis;"

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id                  BIGINT PRIMARY KEY,
    taxon_id            BIGINT,
    lat                 DOUBLE PRECISION,
    lng                 DOUBLE PRECISION,
    observed_on         DATE,
    month               SMALLINT,
    year                SMALLINT,
    quality_grade       TEXT,
    positional_accuracy INTEGER,
    place_guess         TEXT,
    uri                 TEXT,
    obscured            BOOLEAN
);

CREATE TABLE IF NOT EXISTS ingest_log (
    key           TEXT PRIMARY KEY,   -- e.g. "obs:47348:47.6:-122.3:150:2015-01-01:2026-07-11"
    fetched_at    TIMESTAMP,
    row_count     BIGINT,
    lat           DOUBLE PRECISION,
    lng           DOUBLE PRECISION,
    radius_km     DOUBLE PRECISION
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

-- One-time migration: app_location used to be a single global row shared by every visitor
-- (BOOLEAN PK + CHECK enforcing at most one row). The app is now multi-user (anonymous
-- per-device cookie, see api.py), so it needs one row per device instead. Rename the old
-- table out of the way (preserve, don't drop) rather than losing whatever was last saved
-- there; the CREATE TABLE below then claims the original name fresh.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'app_location' AND column_name = 'id' AND data_type = 'boolean'
    ) THEN
        ALTER TABLE app_location RENAME TO app_location_legacy_singleton;
    END IF;
END $$;

-- Per-device "Set location" override: which device set what home/radius, keyed by an opaque
-- anonymous device-id cookie (see api.py resolve_device_id) - no accounts, no login.
CREATE TABLE IF NOT EXISTS app_location (
    device_id TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    lat       DOUBLE PRECISION NOT NULL,
    lng       DOUBLE PRECISION NOT NULL,
    radius_km DOUBLE PRECISION NOT NULL
);

-- Full genus catalog (issue #79): every Fungi genus on iNat, refreshed weekly by
-- `foray genera-refresh` (see foray.inat.iter_fungi_genera). Replaces the old hardcoded
-- 21-genus seed list - `common_name` is NULL for most rows (only well-known genera have an
-- English common name on iNat), so callers must treat `name` (scientific) as the primary
-- label, not an optional fallback.
CREATE TABLE IF NOT EXISTS fungi_genera (
    taxon_id            BIGINT PRIMARY KEY,
    name                TEXT NOT NULL,
    common_name         TEXT,
    observations_count  INTEGER
);

CREATE INDEX IF NOT EXISTS ix_fungi_genera_name ON fungi_genera (name);

-- Per-device genus selection (issue #79 Phase 2): which genera this device wants ranked,
-- keyed by the same anonymous device-id cookie as app_location - but many rows per device
-- (one per selected genus), not app_location's one row per device. A device with zero rows
-- here means "everything nearby" (no filter), not the old curated 21 - see api.py's
-- resolve_genera and scoring.py's taxon_id-filter handling for the empty-list case.
CREATE TABLE IF NOT EXISTS app_genera (
    device_id TEXT NOT NULL,
    taxon_id  BIGINT NOT NULL,
    PRIMARY KEY (device_id, taxon_id)
);

CREATE INDEX IF NOT EXISTS ix_observations_lat_lng ON observations (lat, lng);

-- Scoring's shared _BINNED fragment (scoring.py) filters on taxon_id + observed_on
-- (_recent_counts, recent_observations, alerts) on every live request - without this, each
-- one is a sequential scan over `observations`. (build_phenology's GROUP BY region_id,
-- taxon_id, month is a full aggregate over _BINNED's output regardless, so a plain index on
-- `month` alone wouldn't speed that up - not added.)
CREATE INDEX IF NOT EXISTS ix_observations_taxon_observed ON observations (taxon_id, observed_on);
"""


def connect(conninfo: str = "") -> psycopg.Connection:
    """Open a Postgres connection and ensure the schema exists.

    ``conninfo`` empty (the default) means "use libpq's usual env vars"
    (``PGHOST``/``PGPORT``/``PGUSER``/``PGPASSWORD``/``PGDATABASE``), which is how the
    ECS task, local dev (via ``docker-compose.yml``'s port mapping + a ``.env``), and
    tests (via CI service container env or local PG* vars) are all wired - no
    DSN-building code needed anywhere.
    """
    con = psycopg.connect(conninfo, autocommit=True)
    try:
        con.execute(_ENABLE_POSTGIS)
    except psycopg.Error:
        logger.warning(
            "cache: could not enable postgis (missing extension or insufficient privilege); "
            "the dispersed-camping proxy will be skipped (everything else still works). "
            "Run `CREATE EXTENSION postgis;` as a superuser/rds_superuser to enable it."
        )
    con.execute(SCHEMA)
    con.execute("ALTER TABLE ingest_log ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION")
    con.execute("ALTER TABLE ingest_log ADD COLUMN IF NOT EXISTS lng DOUBLE PRECISION")
    con.execute("ALTER TABLE ingest_log ADD COLUMN IF NOT EXISTS radius_km DOUBLE PRECISION")
    con.execute("ALTER TABLE observations ADD COLUMN IF NOT EXISTS place_guess TEXT")
    con.execute("ALTER TABLE observations ADD COLUMN IF NOT EXISTS uri TEXT")
    con.execute("ALTER TABLE observations ADD COLUMN IF NOT EXISTS obscured BOOLEAN")
    # `taxa` is retired (issue #79 Phase 4): superseded by fungi_genera, the full catalog
    # every name lookup now reads from (see scoring.py's _genus_name_map). Left in place
    # rather than dropped here - a DROP TABLE running unconditionally on every connect() is a
    # disruptive side effect for a rolling deploy where an older instance might still be
    # running against it. Drop it manually (`DROP TABLE IF EXISTS taxa;`) once nothing older
    # than this change is running.
    return con


def upsert_fungi_genera(con: psycopg.Connection, rows: Iterable[dict[str, Any]]) -> None:
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO fungi_genera (taxon_id, name, common_name, observations_count)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (taxon_id) DO UPDATE SET
                name = EXCLUDED.name,
                common_name = EXCLUDED.common_name,
                observations_count = EXCLUDED.observations_count
            """,
            [(row["taxon_id"], row["name"], row.get("common_name"), row.get("observations_count")) for row in rows],
        )


def genus_taxon_ids(con: psycopg.Connection) -> dict[str, int]:
    """Full genus-name -> taxon_id map from the catalog (issue #79 Phase 3: the bulk loader
    matches every catalog genus now, not just the old 21-genus seed list).

    ``name`` has no uniqueness constraint in the schema, so this checks for duplicates
    rather than silently keeping whichever row happens to win a dict build - a silent drop
    here would make the bulk-filter script quietly skip that genus's observations with no
    error to explain why.
    """
    rows = con.execute("SELECT name, taxon_id FROM fungi_genera").fetchall()
    genera: dict[str, int] = {}
    for name, taxon_id in rows:
        if name in genera:
            raise ValueError(f"fungi_genera has duplicate name {name!r} (taxon_ids {genera[name]} and {taxon_id})")
        genera[name] = taxon_id
    return genera


def known_genus_taxon_ids(con: psycopg.Connection) -> set[int]:
    """The full set of catalog taxon_ids, for callers (ingest.py's genus-ancestry resolver)
    that only need membership, not the name map - unlike ``genus_taxon_ids()``, this never
    raises on a duplicate ``name`` (irrelevant here; taxon_id is already the schema's PK)."""
    rows = con.execute("SELECT taxon_id FROM fungi_genera").fetchall()
    return {taxon_id for (taxon_id,) in rows}


def search_fungi_genera(con: psycopg.Connection, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Genus catalog search by scientific or common name, ranked by iNat's observation count.

    Empty ``query`` returns the most-observed genera (a sane browse default), not everything -
    the catalog has ~6,018 rows, too many to dump into a dropdown unfiltered.
    """
    stripped = query.strip()
    if stripped:
        rows = con.execute(
            """
            SELECT taxon_id, name, common_name
            FROM fungi_genera
            WHERE name ILIKE %s OR common_name ILIKE %s
            ORDER BY observations_count DESC NULLS LAST, name
            LIMIT %s
            """,
            [f"%{stripped}%", f"%{stripped}%", limit],
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT taxon_id, name, common_name
            FROM fungi_genera
            ORDER BY observations_count DESC NULLS LAST, name
            LIMIT %s
            """,
            [limit],
        ).fetchall()
    return [{"taxon_id": taxon_id, "name": name, "common_name": common_name} for taxon_id, name, common_name in rows]


def upsert_observations(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Insert observation tuples, backfilling metadata on conflict. Returns rows attempted."""
    if not rows:
        return 0
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO observations
                (id, taxon_id, lat, lng, observed_on, month, year, quality_grade,
                 positional_accuracy, place_guess, uri, obscured)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                taxon_id = COALESCE(EXCLUDED.taxon_id, observations.taxon_id),
                quality_grade = COALESCE(EXCLUDED.quality_grade, observations.quality_grade),
                place_guess = COALESCE(EXCLUDED.place_guess, observations.place_guess),
                uri = COALESCE(EXCLUDED.uri, observations.uri),
                obscured = COALESCE(EXCLUDED.obscured, observations.obscured)
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


def record_ingest(
    con: psycopg.Connection,
    key: str,
    row_count: int,
    *,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO ingest_log (key, fetched_at, row_count, lat, lng, radius_km)
        VALUES (%s, now(), %s, %s, %s, %s)
        ON CONFLICT (key) DO UPDATE SET
            fetched_at = now(),
            row_count = EXCLUDED.row_count,
            lat = EXCLUDED.lat,
            lng = EXCLUDED.lng,
            radius_km = EXCLUDED.radius_km
        """,
        [key, row_count, lat, lng, radius_km],
    )


def observation_count(con: psycopg.Connection) -> int:
    row = con.execute("SELECT count(*) FROM observations").fetchone()
    return int(row[0]) if row else 0


def is_ingested(con: psycopg.Connection, key: str) -> bool:
    row = con.execute("SELECT 1 FROM ingest_log WHERE key = %s", [key]).fetchone()
    return row is not None


def is_area_covered(con: psycopg.Connection, prefix: str, lat: float, lng: float, radius_km: float) -> bool:
    """Check if any previously ingested disk (matching prefix) fully contains the requested disk."""
    rows = con.execute(
        "SELECT lat, lng, radius_km FROM ingest_log WHERE key LIKE %s AND lat IS NOT NULL",
        [f"{prefix}%"],
    ).fetchall()
    for row_lat, row_lng, row_radius in rows:
        dist = haversine_km(row_lat, row_lng, lat, lng)
        if dist + radius_km <= row_radius:
            return True
    return False


def latest_obs_date(con: psycopg.Connection, token: int | str, lat: float, lng: float, radius_km: float) -> str | None:
    """Latest end-date from ingest_log for a home-radius pull matching ``token`` (a taxon_id,
    or "fungi" for the whole-kingdom ingest, see ingest.py)."""
    rows = con.execute(
        "SELECT key, lat AS rlat, lng AS rlng, radius_km AS rr FROM ingest_log WHERE key LIKE %s AND lat IS NOT NULL",
        [f"obs:{token}:%"],
    ).fetchall()
    if not rows:
        return None
    dates: list[str] = []
    for key, rlat, rlng, rr in rows:
        dist = haversine_km(rlat, rlng, lat, lng)
        if dist + radius_km <= rr:
            dates.append(key.split(":")[-1])
    if not dates:
        return None
    return max(dates)


def latest_obs_date_by_place(con: psycopg.Connection, token: int | str, place_id: int) -> str | None:
    """Return the latest end-date from ingest_log for a place_id-based pull, or None."""
    row = con.execute(
        "SELECT max(split_part(key, ':', 6)) FROM ingest_log WHERE key LIKE %s",
        [f"obs:{token}:place:{place_id}:%"],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return row[0]


def load_location(con: psycopg.Connection, device_id: str) -> dict[str, Any] | None:
    """This device's "Set location" override, if one has been saved. `None` = use the default."""
    row = con.execute("SELECT name, lat, lng, radius_km FROM app_location WHERE device_id = %s", [device_id]).fetchone()
    if row is None:
        return None
    name, lat, lng, radius_km = row
    return {"name": name, "lat": lat, "lng": lng, "radius_km": radius_km}


def save_location(
    con: psycopg.Connection, *, device_id: str, name: str, lat: float, lng: float, radius_km: float
) -> None:
    con.execute(
        """
        INSERT INTO app_location (device_id, name, lat, lng, radius_km)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (device_id) DO UPDATE SET
            name = EXCLUDED.name,
            lat = EXCLUDED.lat,
            lng = EXCLUDED.lng,
            radius_km = EXCLUDED.radius_km
        """,
        [device_id, name, lat, lng, radius_km],
    )


def load_genera(con: psycopg.Connection, device_id: str) -> list[int]:
    """This device's selected genus taxon_ids. Empty means "everything nearby", not "none"."""
    rows = con.execute("SELECT taxon_id FROM app_genera WHERE device_id = %s", [device_id]).fetchall()
    return [row[0] for row in rows]


def list_selected_genera(con: psycopg.Connection, device_id: str) -> list[dict[str, Any]]:
    """This device's selected genera with their catalog names, for chip display."""
    rows = con.execute(
        """
        SELECT fungi_genera.taxon_id, fungi_genera.name, fungi_genera.common_name
        FROM app_genera
        JOIN fungi_genera ON fungi_genera.taxon_id = app_genera.taxon_id
        WHERE app_genera.device_id = %s
        ORDER BY fungi_genera.name
        """,
        [device_id],
    ).fetchall()
    return [{"taxon_id": taxon_id, "name": name, "common_name": common_name} for taxon_id, name, common_name in rows]


def add_genus(con: psycopg.Connection, device_id: str, taxon_id: int) -> None:
    con.execute(
        "INSERT INTO app_genera (device_id, taxon_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        [device_id, taxon_id],
    )


def remove_genus(con: psycopg.Connection, device_id: str, taxon_id: int) -> None:
    con.execute("DELETE FROM app_genera WHERE device_id = %s AND taxon_id = %s", [device_id, taxon_id])
