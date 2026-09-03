"""Postgres cache: schema, idempotent upserts, and the ingest log.

Observations are keyed by iNat id, so re-ingesting the same window is a no-op
(``ON CONFLICT DO NOTHING``). Region binning (grid cell) is derived in SQL from
lat/lng and ``cell_deg`` so it is never stored redundantly.

Connections are opened with ``autocommit=True`` (nothing here was written against explicit
transactions) - callers that need atomicity across statements (e.g.
``regions.build_phenology``'s drop+rebuild) wrap them in an explicit
``with con.transaction():`` block instead.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, LiteralString

import psycopg

from foray.geo import haversine_km

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id                  BIGINT PRIMARY KEY,
    taxon_id            BIGINT,
    lat                 DOUBLE PRECISION,
    lng                 DOUBLE PRECISION,
    observed_on         DATE,
    month               SMALLINT,
    quality_grade       TEXT,
    positional_accuracy INTEGER,
    place_guess         TEXT,
    uri                 TEXT,
    obscured            BOOLEAN,
    elevation_m         INTEGER,  -- ground elevation (issue #36), enriched post-ingest via Open-Meteo
    precip_7d_mm        DOUBLE PRECISION,  -- rain in the 7 d before observed_on (issue #226), post-ingest
    precip_30d_mm       DOUBLE PRECISION   -- rain in the 30 d before observed_on (issue #226), post-ingest
);

CREATE TABLE IF NOT EXISTS ingest_log (
    key           TEXT PRIMARY KEY,   -- e.g. "obs:47348:47.6:-122.3:150:2015-01-01:2026-07-11"
    fetched_at    TIMESTAMP,
    row_count     BIGINT,
    lat           DOUBLE PRECISION,
    lng           DOUBLE PRECISION,
    radius_km     DOUBLE PRECISION
);

-- Campsites: developed campgrounds (Recreation.gov RIDB) plus OSM-reported dispersed-camping
-- sites (tourism=camp_site/camp_pitch, backcountry=yes - see dispersed.py). Keyed by
-- "{source}:{source_id}" so re-ingesting the same area is a no-op. `free` is nullable on
-- purpose: we only assert free when the source says so, never guess.
CREATE TABLE IF NOT EXISTS campsites (
    id          TEXT PRIMARY KEY,    -- "{source}:{source_id}", e.g. "ridb:250018", "osm:way/42"
    name        TEXT,
    kind        TEXT,                -- "campground" (RIDB), "reported" (OSM)
    fee         TEXT,                -- raw fee description when known, else NULL
    free        BOOLEAN,             -- TRUE on an explicit no-fee signal (RIDB/OSM tag), else NULL
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

-- Wildfire perimeters + points (issue #227). An active fire and a recent burn scar are the
-- same polygon at different life stages, so one table holds both, split by `source_key` into
-- two refresh lanes that never clobber each other:
--   'wfigs_active'      status='active'      - fast cadence, REPLACE semantics (rows gone from
--                                              the source each refresh are deleted, not kept)
--   'perimeter_history' status='historical'  - slow cadence, plain upsert; last 3 completed
--                                              fire years + current (the morel productivity curve)
-- Geometry is GeoJSON *text* + bbox + representative center, same as public_land/trails - the
-- read/map path needs no PostGIS. Informational only: links the official incident page, never
-- asserts a road/forest closure (see AGENTS.md). MTBS severity columns stay NULL until MTBS
-- publishes (~1.5-2 yr after the season); the layer works without them.
CREATE TABLE IF NOT EXISTS fire_perimeters (
    id                       TEXT PRIMARY KEY,   -- "{source_key}:{feature_id}"
    source_key               TEXT,               -- 'wfigs_active' | 'wfigs_points' | 'perimeter_history'
    feature_id               TEXT,               -- stable per-feature id from the source
    irwin_id                 TEXT,               -- IRWIN incident id where available (MTBS/dedupe join key)
    name                     TEXT,
    status                   TEXT,               -- 'active' | 'historical'
    fire_year                INTEGER,
    discovery_date           DATE,
    percent_contained        DOUBLE PRECISION,   -- active only
    gis_acres                DOUBLE PRECISION,
    incident_url             TEXT,               -- official InciWeb / NIFC incident page
    severity_unburned_acres  DOUBLE PRECISION,   -- MTBS enrichment, NULL until published
    severity_low_acres       DOUBLE PRECISION,
    severity_moderate_acres  DOUBLE PRECISION,
    severity_high_acres      DOUBLE PRECISION,
    dominant_severity        TEXT,               -- 'low' | 'moderate' | 'high' | NULL
    mtbs_fire_id             TEXT,
    is_point                 BOOLEAN,            -- true for a WFIGS location with no perimeter yet
    min_lat                  DOUBLE PRECISION,
    min_lng                  DOUBLE PRECISION,
    max_lat                  DOUBLE PRECISION,
    max_lng                  DOUBLE PRECISION,
    center_lat               DOUBLE PRECISION,
    center_lng               DOUBLE PRECISION,
    geojson                  TEXT,
    fetched_at               TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_fire_perimeters_bbox ON fire_perimeters (min_lat, max_lat, min_lng, max_lng);
CREATE INDEX IF NOT EXISTS ix_fire_perimeters_lane ON fire_perimeters (source_key);

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
-- `foray genera-refresh` (see foray.sources.inat.iter_fungi_genera). Replaces the old hardcoded
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

-- Destination-card place titling (issue #206): caches one reverse-geocode result per grid
-- region forever - regions are a fixed grid (scoring.py's cell_deg binning), so a region's
-- centroid never moves and its notable place name never needs re-resolving. `place_name` is
-- nullable on purpose: a row existing means "already looked up", regardless of whether a
-- notable place was found - so a remote/rural region with no notable place nearby is cached
-- as a negative result instead of re-hitting Nominatim on every destinations refresh.
CREATE TABLE IF NOT EXISTS region_places (
    region_id  TEXT PRIMARY KEY,
    place_name TEXT
);

-- Daily precipitation cache (issue #226). One row per grid cell per day - `cell_id` is the
-- same "{ilat}_{ilng}" key `regions`/`phenology` derive in SQL (foray.geo.grid_cell), so the
-- weather geography reuses the region grid rather than inventing a second one. Raw per-day
-- values (mm) so the derived windows (7/14/30 d) are the only thing schema locks in. `precip_mm`
-- is nullable: Open-Meteo's ERA5 archive runs ~5-7 days behind and returns null for a day it
-- has no value for yet - stored as NULL and retried, never coerced to 0.
CREATE TABLE IF NOT EXISTS precip_daily (
    cell_id     TEXT NOT NULL,
    date        DATE NOT NULL,
    precip_mm   DOUBLE PRECISION,   -- daily precipitation_sum, mm; NULL = source had no value yet
    source      TEXT,               -- "open-meteo-archive" | "open-meteo-forecast"
    fetched_at  TIMESTAMPTZ,
    PRIMARY KEY (cell_id, date)
);

-- Recent-rainfall-per-destination layer (issue #226 Part 2). One row per active region cell,
-- refreshed on its own scheduler cadence (FORAY_PRECIP_INTERVAL_HOURS, default 24) from
-- Open-Meteo's forecast API (past_days). Trailing-window sums ending "today"; informational
-- only, no scoring (deferred).
CREATE TABLE IF NOT EXISTS precipitation (
    region_id     TEXT PRIMARY KEY,
    precip_7d_mm  DOUBLE PRECISION,
    precip_14d_mm DOUBLE PRECISION,
    precip_30d_mm DOUBLE PRECISION,
    updated_at    TIMESTAMPTZ
);

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

-- trails_near's/camps_near's bbox prefilter (`min_lat <= ? AND max_lat >= ? AND ...`) was a full
-- sequential scan without these - fine for the map's one-off "trails near a click", but
-- plan_route calls trails_near/camps_near per candidate stop, and trails alone had 1M+ rows in
-- production - measured ~350ms/call unindexed vs ~50ms with this index.
CREATE INDEX IF NOT EXISTS ix_trails_bbox ON trails (min_lat, max_lat, min_lng, max_lng);
CREATE INDEX IF NOT EXISTS ix_campsites_lat_lng ON campsites (lat, lng);

-- Scoring's shared BINNED fragment (_sql.py) filters on quality_grade = 'research' then
-- taxon_id + observed_on (recent_counts, recent_observations, alerts) on every live request.
-- The partial index keyed to that filter lives in apply_schema's CONCURRENTLY block (built
-- without a write lock, and it supersedes the old non-partial ix_observations_taxon_observed,
-- which that block drops).

-- Every ingest_log coverage check (is_area_covered, latest_obs_date, latest_obs_date_by_place)
-- is a `key LIKE 'prefix%'` scan (issue #112). Postgres can only use a plain btree index for a
-- prefix LIKE under the `C` collation - under the default locale-aware collation this table's
-- managed-Postgres image actually uses, it degrades to a sequential scan. `text_pattern_ops`
-- builds a pattern-matching index regardless of collation, so the prefix scan stays an index
-- scan as ingest_log grows (one row per taxon/region/window per scheduler cycle).
CREATE INDEX IF NOT EXISTS ix_ingest_log_key_pattern ON ingest_log (key text_pattern_ops);

-- apply_schema's fast-path sentinel (see SCHEMA_VERSION): stores the SCHEMA revision last
-- fully applied so a connect() with everything current skips re-executing this whole string
-- (and the CONCURRENTLY probe) on every CLI call and cron tick.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Bump whenever the SCHEMA string above OR the CONCURRENTLY index set in apply_schema changes,
# so a running instance re-executes them once on its next apply_schema. (New _MIGRATIONS
# entries are tracked separately by version and don't need a bump.)
SCHEMA_VERSION = 2

# Fixed advisory-lock key so two processes starting together (API + scheduler) serialize on
# the full apply_schema path instead of racing CREATE INDEX CONCURRENTLY.
_SCHEMA_LOCK_KEY = 4915623

# CONCURRENTLY-built indexes, applied outside any transaction (autocommit) so they never hold a
# write lock on the ~1.9M-row observations table during a rolling deploy. Each is attempted
# independently and a failure is logged, not raised: IF NOT EXISTS / IF EXISTS makes them
# idempotent, but two instances starting together can still race (one loses), and none of these
# is a correctness dependency - only a query-speed optimization.
_CONCURRENT_INDEXES: list[LiteralString] = [
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_observations_revalidated_at ON observations (revalidated_at)",
    # Supersedes the old non-partial ix_observations_taxon_observed: BINNED always filters
    # quality_grade = 'research' first, so the partial index is smaller and better matched.
    # Create the replacement first, drop the old one only after - a failed/cancelled build must
    # not leave the table with neither index.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_observations_taxon_observed_research "
    "ON observations (taxon_id, observed_on) WHERE quality_grade = 'research'",
    "DROP INDEX CONCURRENTLY IF EXISTS ix_observations_taxon_observed",
    # Backfill-queue scans (observations_missing_elevation / observations_missing_precip): the
    # partial predicate matches the WHERE clause so the queue is an index scan over just the
    # pending rows, not a seq scan of the whole table. Keyed on the queue's ORDER BY column.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_observations_elevation_missing ON observations (id) "
    "WHERE elevation_m IS NULL AND quality_grade = 'research' AND NOT COALESCE(obscured, false)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_observations_precip_missing ON observations (observed_on) "
    "WHERE (precip_7d_mm IS NULL OR precip_30d_mm IS NULL) "
    "AND quality_grade = 'research' AND NOT COALESCE(obscured, false)",
    # PostGIS Phase 0 GIST indexes - one per geom column. On the 1-vCPU box the observations
    # (~1.9M points) and trails (~1M lines) builds are slow even CONCURRENTLY; schedule those
    # into a maintenance window or accept a slow first post-deploy apply_schema.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_campsites_geom ON campsites USING GIST (geom)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_observations_geom ON observations USING GIST (geom)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_trails_geom ON trails USING GIST (geom)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_public_land_geom ON public_land USING GIST (geom)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_fire_perimeters_geom ON fire_perimeters USING GIST (geom)",
]

# Schema changes past the initial CREATE TABLE/INDEX IF NOT EXISTS baseline above, applied in
# order and recorded in `schema_migrations` (issue #117) so a connect() with everything already
# applied is one SELECT instead of re-running the growing list of ALTER TABLE statements below -
# each is individually idempotent, but that stops scaling as more get added over the project's
# life. New migrations: append a new (version, statement) tuple, never edit/reorder an existing
# one (already-applied versions are looked up by number, not by content).
_MIGRATIONS: list[tuple[int, LiteralString]] = [
    (1, "ALTER TABLE ingest_log ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION"),
    (2, "ALTER TABLE ingest_log ADD COLUMN IF NOT EXISTS lng DOUBLE PRECISION"),
    (3, "ALTER TABLE ingest_log ADD COLUMN IF NOT EXISTS radius_km DOUBLE PRECISION"),
    (4, "ALTER TABLE observations ADD COLUMN IF NOT EXISTS place_guess TEXT"),
    (5, "ALTER TABLE observations ADD COLUMN IF NOT EXISTS uri TEXT"),
    (6, "ALTER TABLE observations ADD COLUMN IF NOT EXISTS obscured BOOLEAN"),
    (7, "ALTER TABLE observations ADD COLUMN IF NOT EXISTS revalidated_at TIMESTAMPTZ"),
    # issue #117 finding 13: `year` was write-only (populated at ingest, upserted at resync,
    # never read by any query) - a stored duplicate of `EXTRACT(YEAR FROM observed_on)` that
    # cost a write on every ingested row for a value nothing consumed. Fully recoverable from
    # observed_on if a future feature ever needs year-based filtering.
    (8, "ALTER TABLE observations DROP COLUMN IF EXISTS year"),
    # issue #36: per-observation ground elevation, enriched after ingest from Open-Meteo's DEM
    # (see ingest.backfill_elevations). NULL = not yet looked up.
    (9, "ALTER TABLE observations ADD COLUMN IF NOT EXISTS elevation_m INTEGER"),
    # issue #242 Part 5: drop two retired artifacts that were left in place with manual-TODO
    # comments rather than a migration. `taxa` (issue #79 Phase 4) was superseded by
    # fungi_genera - every name lookup reads the full catalog now (regions._genus_name_map);
    # nothing has referenced `taxa` since. `app_location_legacy_singleton` is the pre-multi-user
    # single-row `app_location` table, renamed out of the way (not dropped) by the SCHEMA block
    # above when the per-device table was introduced - its one row was a stale global home that
    # no code path reads. By the time this migration ships, every running instance is on the
    # post-#250 code that touches neither table, so the drop is safe on a rolling deploy.
    (10, "DROP TABLE IF EXISTS taxa"),
    (11, "DROP TABLE IF EXISTS app_location_legacy_singleton"),
    # issue #226: antecedent rainfall per observation, summed from the precip_daily cache over
    # the 7 and 30 days before observed_on (see ingest.backfill_precip). NULL = not yet
    # enriched, or a day inside the window that Open-Meteo's ERA5 archive still returns null for
    # (a partial sum is never recorded).
    (12, "ALTER TABLE observations ADD COLUMN IF NOT EXISTS precip_7d_mm DOUBLE PRECISION"),
    (13, "ALTER TABLE observations ADD COLUMN IF NOT EXISTS precip_30d_mm DOUBLE PRECISION"),
    # --- PostGIS Phase 0 (additive; no read-path change yet) -------------------------------
    # A real spatial column + GIST index per table replaces the "bbox columns then haversine
    # in a Python loop" pattern the hot read paths use. `geography(*, 4326)` is sphere-based
    # metres - the same model as `foray.geo.haversine_km`, so results don't shift. Order
    # matters: the extension must exist before any column of its type.
    (14, "CREATE EXTENSION IF NOT EXISTS postgis"),
    # Point tables. `geom` is nullable and populated by the BEFORE trigger below (functions in
    # migrations 20/21, triggers wired in 22), not GENERATED ALWAYS AS ... STORED - a stored
    # generated column's ADD COLUMN forces a full table rewrite under ACCESS EXCLUSIVE,
    # unacceptable on observations' ~1.9M rows.
    (15, "ALTER TABLE campsites ADD COLUMN IF NOT EXISTS geom geography(Point, 4326)"),
    (16, "ALTER TABLE observations ADD COLUMN IF NOT EXISTS geom geography(Point, 4326)"),
    # Line/polygon tables (geometry type varies: LineString / MultiLineString / Polygon / Point).
    (17, "ALTER TABLE trails ADD COLUMN IF NOT EXISTS geom geography(Geometry, 4326)"),
    (18, "ALTER TABLE public_land ADD COLUMN IF NOT EXISTS geom geography(Geometry, 4326)"),
    (19, "ALTER TABLE fire_perimeters ADD COLUMN IF NOT EXISTS geom geography(Geometry, 4326)"),
    # Point-table trigger: derive geom from lat/lng. Skips the recompute when an UPDATE leaves
    # both coordinates untouched (e.g. mark_revalidated stamping revalidated_at) so bulk
    # resync passes don't pay for it. NULL coords -> NULL geom.
    (
        20,
        """
        CREATE OR REPLACE FUNCTION foray_geom_from_latlng() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND NEW.lat IS NOT DISTINCT FROM OLD.lat
               AND NEW.lng IS NOT DISTINCT FROM OLD.lng THEN
                RETURN NEW;
            END IF;
            IF NEW.lat IS NOT NULL AND NEW.lng IS NOT NULL THEN
                NEW.geom := ST_SetSRID(ST_MakePoint(NEW.lng, NEW.lat), 4326)::geography;
            ELSE
                NEW.geom := NULL;
            END IF;
            RETURN NEW;
        END;
        $$;
        """,
    ),
    # Layer-table trigger: derive geom from the GeoJSON text. ST_MakeValid is geometry-only,
    # so validate then cast. A malformed / empty feature leaves geom NULL and logs a WARNING
    # rather than aborting the whole upsert batch.
    (
        21,
        """
        CREATE OR REPLACE FUNCTION foray_geom_from_geojson() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.geojson IS NOT DISTINCT FROM OLD.geojson THEN
                RETURN NEW;
            END IF;
            IF NEW.geojson IS NULL THEN
                NEW.geom := NULL;
                RETURN NEW;
            END IF;
            BEGIN
                NEW.geom := ST_MakeValid(ST_GeomFromGeoJSON(NEW.geojson))::geography;
            EXCEPTION WHEN others THEN
                NEW.geom := NULL;
                RAISE WARNING 'foray_geom_from_geojson: bad geometry for %.%: %',
                    TG_TABLE_NAME, NEW.id, SQLERRM;
            END;
            RETURN NEW;
        END;
        $$;
        """,
    ),
    (
        22,
        """
        CREATE OR REPLACE TRIGGER trg_campsites_geom BEFORE INSERT OR UPDATE ON campsites
            FOR EACH ROW EXECUTE FUNCTION foray_geom_from_latlng();
        CREATE OR REPLACE TRIGGER trg_observations_geom BEFORE INSERT OR UPDATE ON observations
            FOR EACH ROW EXECUTE FUNCTION foray_geom_from_latlng();
        CREATE OR REPLACE TRIGGER trg_trails_geom BEFORE INSERT OR UPDATE ON trails
            FOR EACH ROW EXECUTE FUNCTION foray_geom_from_geojson();
        CREATE OR REPLACE TRIGGER trg_public_land_geom BEFORE INSERT OR UPDATE ON public_land
            FOR EACH ROW EXECUTE FUNCTION foray_geom_from_geojson();
        CREATE OR REPLACE TRIGGER trg_fire_perimeters_geom BEFORE INSERT OR UPDATE ON fire_perimeters
            FOR EACH ROW EXECUTE FUNCTION foray_geom_from_geojson();
        """,
    ),
]

_MIGRATION_VERSIONS = [version for version, _ in _MIGRATIONS]


def connect(conninfo: str = "") -> psycopg.Connection:
    """Open a Postgres connection and ensure the schema exists.

    ``conninfo`` empty (the default) means "use libpq's usual env vars"
    (``PGHOST``/``PGPORT``/``PGUSER``/``PGPASSWORD``/``PGDATABASE``), which is how the
    deployed container (ansible-managed env file), local dev (via
    ``docker-compose.yml``'s port mapping + a ``.env``), and tests (via CI service
    container env or local PG* vars) are all wired - no DSN-building code needed anywhere.
    """
    con = psycopg.connect(conninfo, autocommit=True)
    apply_schema(con)
    return con


@contextmanager
def connection(con: psycopg.Connection | None = None) -> Iterator[psycopg.Connection]:
    """Yield a usable connection, closing it on exit only if this opened it.

    Collapses the ``own_con = con is None`` / ``try: ... finally: if own_con: db.close()``
    dance every ingest entrypoint repeats: ``with cache.connection(con) as db:`` passes a
    caller-supplied ``con`` straight through (caller still owns its lifecycle) and otherwise
    opens a fresh one and guarantees it is closed.
    """
    if con is not None:
        yield con
        return
    fresh = connect()
    try:
        yield fresh
    finally:
        fresh.close()


def upsert_rows(
    con: psycopg.Connection,
    table: LiteralString,
    columns: Sequence[LiteralString],
    rows: Sequence[tuple[Any, ...]],
    *,
    conflict: LiteralString = "id",
    coalesce: Collection[str] = (),
) -> int:
    """Bulk ``INSERT ... ON CONFLICT (<conflict>) DO UPDATE`` for a list of positional tuples.

    Every non-conflict column is refreshed from ``EXCLUDED`` on conflict; columns named in
    ``coalesce`` are wrapped ``COALESCE(EXCLUDED.col, <table>.col)`` so a partial re-upsert
    that doesn't carry every column can't blank a previously-healed value (the guard
    ``upsert_observations`` needs). Returns the number of rows attempted.

    ``table``/``columns``/``conflict`` are typed ``LiteralString`` - they come from this
    module's own call sites, never request data - so composing them into the statement text is
    safe. Row *values* are always parameterised by ``executemany``.
    """
    if not rows:
        return 0
    placeholders = ", ".join(["%s"] * len(columns))
    assignments = ", ".join(
        f"{col} = COALESCE(EXCLUDED.{col}, {table}.{col})" if col in coalesce else f"{col} = EXCLUDED.{col}"
        for col in columns
        if col != conflict
    )
    sql: LiteralString = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) DO UPDATE SET {assignments}"
    )
    with con.cursor() as cur:
        cur.executemany(sql, rows)
    return len(rows)


def copy_upsert(
    con: psycopg.Connection,
    table: LiteralString,
    columns: Sequence[LiteralString],
    rows: Sequence[tuple[Any, ...]],
    *,
    conflict: LiteralString = "id",
    coalesce: Collection[str] = (),
) -> int:
    """Same result as :func:`upsert_rows` - bulk ``INSERT ... ON CONFLICT DO UPDATE`` with the
    per-column ``COALESCE`` guards - but the rows stream in through ``COPY`` into a session-temp
    staging table and land with one set-based ``INSERT ... SELECT``. For ingest-sized batches
    (5k observation tuples at a time) that beats ``executemany``'s per-row round-trips
    handily - the same win the DEM elevation backfill measured (#238).

    A conflict key repeated within one batch is collapsed to its *last* occurrence before the
    COPY - matching ``executemany``'s row-by-row "last write wins", and keeping the
    ``INSERT ... SELECT`` from hitting "ON CONFLICT DO UPDATE command cannot affect row a
    second time" if a paginated source ever straddles the same id twice. ``conflict`` must be a
    single column (it always is here); the dedupe keys on that column's position.

    ``table``/``columns``/``conflict`` are ``LiteralString`` from this module's own call sites;
    the row values ride through ``COPY`` as data. Returns the number of rows attempted.
    """
    if not rows:
        return 0
    key_idx = list(columns).index(conflict)
    deduped = list({row[key_idx]: row for row in rows}.values())  # dict keeps last per key, in order
    collist = ", ".join(columns)
    assignments = ", ".join(
        f"{col} = COALESCE(EXCLUDED.{col}, {table}.{col})" if col in coalesce else f"{col} = EXCLUDED.{col}"
        for col in columns
        if col != conflict
    )
    create_stg: LiteralString = (
        f"CREATE TEMP TABLE _copy_stg ON COMMIT DROP AS SELECT {collist} FROM {table} WITH NO DATA"
    )
    copy_in: LiteralString = f"COPY _copy_stg ({collist}) FROM STDIN"
    insert_select: LiteralString = (
        f"INSERT INTO {table} ({collist}) SELECT {collist} FROM _copy_stg "
        f"ON CONFLICT ({conflict}) DO UPDATE SET {assignments}"
    )
    with con.transaction(), con.cursor() as cur:
        cur.execute(create_stg)
        with cur.copy(copy_in) as copy:
            for row in deduped:
                copy.write_row(row)
        cur.execute(insert_select)
    return len(rows)


def _schema_is_current(con: psycopg.Connection) -> bool:
    """True when ``SCHEMA`` at ``SCHEMA_VERSION`` and every ``_MIGRATIONS`` entry are already
    applied on ``con`` - the fast-path guard that lets ``apply_schema`` skip re-executing the
    whole ``SCHEMA`` string (and the CONCURRENTLY probe) on every CLI call and cron tick."""
    reg = con.execute("SELECT to_regclass('meta'), to_regclass('schema_migrations')").fetchone()
    if reg is None or reg[0] is None or reg[1] is None:
        return False
    row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None or row[0] != str(SCHEMA_VERSION):
        return False
    # Count rather than max(version): a deliberately-cleared middle version (a repair, or the
    # apply_schema test) leaves max() unchanged but must still trigger the full path.
    applied = con.execute(
        "SELECT count(*) FROM schema_migrations WHERE version = ANY(%s)", [_MIGRATION_VERSIONS]
    ).fetchone()
    return applied is not None and applied[0] == len(_MIGRATIONS)


def apply_schema(con: psycopg.Connection) -> None:
    """Ensure the baseline schema, the ``_MIGRATIONS`` chain, and the CONCURRENTLY-built
    indexes are all present on ``con``.

    Split out of ``connect()`` because the API server never calls ``connect()`` - it runs a
    psycopg_pool and used to only apply ``SCHEMA`` at lifespan startup, so a column added by a
    later migration (e.g. ``observations.elevation_m``, migration 9) never landed on a
    pre-existing prod table until an out-of-process CLI/cron run happened to call ``connect()``.
    The server lifespan now calls this instead. ``con`` must be autocommit (CREATE INDEX
    CONCURRENTLY cannot run in a transaction block).

    Fast-path: when :func:`_schema_is_current` says everything is already applied, this is two
    cheap SELECTs and returns - it used to re-run the entire ``SCHEMA`` string plus a
    ``CREATE INDEX CONCURRENTLY`` probe on every connect (6+/day from cron alone)."""
    if _schema_is_current(con):
        return
    # Serialize the full path: two instances starting together (API + scheduler) would
    # otherwise race CREATE INDEX CONCURRENTLY. Session-level lock (autocommit -> no xact to
    # scope it to); released in the finally.
    con.execute("SELECT pg_advisory_lock(%s)", [_SCHEMA_LOCK_KEY])
    try:
        if _schema_is_current(con):
            return
        con.execute(SCHEMA)
        con.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {row[0] for row in con.execute("SELECT version FROM schema_migrations").fetchall()}
        for version, statement in _MIGRATIONS:
            if version in applied:
                continue
            con.execute(statement)
            # ON CONFLICT DO NOTHING: two processes can both see `version` as unapplied and
            # race to run it. The migration statements are idempotent (IF NOT EXISTS /
            # IF EXISTS), so that's harmless - but a plain INSERT would raise a primary-key
            # violation and abort startup for the loser.
            con.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
                [version],
            )
        # CONCURRENTLY (see _CONCURRENT_INDEXES): a plain CREATE INDEX on prod's ~1.9M-row
        # observations table would hold a write lock for the build's duration. Safe outside an
        # explicit transaction block since `con` is autocommit. A race with another starting
        # instance is caught and logged, not raised - none of these is a correctness dependency.
        indexes_ok = True
        for statement in _CONCURRENT_INDEXES:
            try:
                con.execute(statement)
            except psycopg.Error:
                indexes_ok = False
                logger.warning(
                    "cache: could not apply %r (likely a concurrent CREATE/DROP INDEX race with "
                    "another starting instance) - queries still work, just without this index "
                    "until a later apply_schema retries it.",
                    statement,
                )
        # Only stamp the fast-path sentinel once every CONCURRENTLY statement landed - otherwise
        # _schema_is_current() would short-circuit the next connect() and the failed index would
        # never be retried, contradicting the warning above. A failure leaves the sentinel behind
        # (or absent) so the next apply_schema takes the full path again; the SCHEMA re-exec and
        # migration loop are idempotent, so the retry is cheap and safe.
        if indexes_ok:
            con.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                [str(SCHEMA_VERSION)],
            )
        # `taxa` and `app_location_legacy_singleton` (both retired) are dropped by migrations 10
        # and 11 above, not here - a bare DROP on every connect() would be a disruptive side
        # effect, whereas the migration chain runs each statement exactly once and records it.
    finally:
        con.execute("SELECT pg_advisory_unlock(%s)", [_SCHEMA_LOCK_KEY])


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
    """Insert observation tuples, backfilling metadata on conflict. Returns rows attempted.

    Every column is refreshed on conflict (not just taxon_id/quality_grade/etc) - a cached
    observation is only ever touched again by a later incremental window or by
    ``ingest.revalidate``, so if a re-fetch happens at all its lat/lng/observed_on/
    positional_accuracy must win too, or a since-corrected location/accuracy on iNat's side
    stays wrong here forever. COALESCE still guards against a partial-loader re-upsert (one
    that doesn't carry every column) blanking out a previously-healed value.
    """
    columns: tuple[LiteralString, ...] = (
        "id",
        "taxon_id",
        "lat",
        "lng",
        "observed_on",
        "month",
        "quality_grade",
        "positional_accuracy",
        "place_guess",
        "uri",
        "obscured",
    )
    return copy_upsert(con, "observations", columns, rows, coalesce=set(columns) - {"id"})


def suspect_genus_taxon_ids(con: psycopg.Connection, ratio: float = 3.0) -> list[int]:
    """Genus taxon_ids whose cached observation count has drifted implausibly far above iNat's
    own live count for that genus (``fungi_genera.observations_count``, refreshed weekly by
    ``foray genera-refresh`` - no iNat call needed here, this is DB-only).

    This is the fingerprint of a cross-kingdom name homonym: a fungal genus taxon_id that
    happens to share its scientific name with an established, common, completely unrelated
    animal genus (e.g. fungal *Olla* vs. the ladybug genus *Olla*). A legitimate genus can only
    ever have as many cached rows as fit inside our home-radius/region scoping, which is always
    a fraction of iNat's global total - so "cached far exceeds live" only happens when
    observations of the *other* (non-fungal) taxon got attributed to this taxon_id at ingest
    time and never got re-synced since (see ``ingest.revalidate`` - a live census found 19 such
    genera accounting for ~24k/1.97M cached rows).
    """
    rows = con.execute(
        """
        SELECT o.taxon_id
        FROM observations o
        JOIN fungi_genera g ON g.taxon_id = o.taxon_id
        GROUP BY o.taxon_id, g.observations_count
        HAVING count(*) > %s * COALESCE(g.observations_count, 0)
        """,
        [ratio],
    ).fetchall()
    return [taxon_id for (taxon_id,) in rows]


def observation_ids_for_genus(con: psycopg.Connection, taxon_id: int) -> list[int]:
    rows = con.execute("SELECT id FROM observations WHERE taxon_id = %s", [taxon_id]).fetchall()
    return [row[0] for row in rows]


def delete_observations(con: psycopg.Connection, ids: Sequence[int]) -> int:
    """Remove cached rows by id (``ingest.revalidate`` purging observations no longer Fungi).
    Returns rows attempted (not the actual delete count - ids may already be gone)."""
    if not ids:
        return 0
    con.execute("DELETE FROM observations WHERE id = ANY(%s)", [list(ids)])
    return len(ids)


def stale_observation_ids(con: psycopg.Connection, limit: int) -> list[int]:
    """The next batch for ``ingest.resync``'s full-table grind: never-live-checked rows first
    (``revalidated_at IS NULL`` - every bulk-historical-import row starts this way), then the
    longest-since-checked. Unlike ``suspect_genus_taxon_ids`` (targeted at one known failure
    pattern), this is what eventually re-verifies every column of every cached row against iNat,
    including ``obscured`` (never set by the bulk import) and misidentifications too rare within
    their genus to trip the ratio-based suspect check.
    """
    rows = con.execute(
        "SELECT id FROM observations ORDER BY revalidated_at ASC NULLS FIRST LIMIT %s",
        [limit],
    ).fetchall()
    return [row[0] for row in rows]


def observation_taxon_ids(con: psycopg.Connection, ids: Sequence[int]) -> dict[int, int]:
    """Current cached taxon_id for each id, so a resync/revalidate pass can tell a genuine genus
    reassignment (new taxon_id != this) apart from a same-genus refresh."""
    if not ids:
        return {}
    rows = con.execute("SELECT id, taxon_id FROM observations WHERE id = ANY(%s)", [list(ids)]).fetchall()
    return dict(rows)


def mark_revalidated(con: psycopg.Connection, ids: Sequence[int]) -> None:
    """Stamp ``revalidated_at = now()`` on ids that were just live-checked (whether or not they
    changed) - advances the ``stale_observation_ids`` cursor past them."""
    if not ids:
        return
    con.execute("UPDATE observations SET revalidated_at = now() WHERE id = ANY(%s)", [list(ids)])


def upsert_campsites(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert campsite tuples, refreshing existing rows in place. Returns rows attempted.

    Each tuple is (id, name, kind, fee, free, lat, lng, source, url).
    """
    columns: tuple[LiteralString, ...] = ("id", "name", "kind", "fee", "free", "lat", "lng", "source", "url")
    return upsert_rows(con, "campsites", columns, rows)


def upsert_public_land(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert public-land polygons, refreshing existing rows in place. Returns rows attempted.

    Each tuple is (id, agency, unit, source, url, min_lat, min_lng, max_lat, max_lng, geojson).
    """
    columns: tuple[LiteralString, ...] = (
        "id",
        "agency",
        "unit",
        "source",
        "url",
        "min_lat",
        "min_lng",
        "max_lat",
        "max_lng",
        "geojson",
    )
    return upsert_rows(con, "public_land", columns, rows)


def upsert_trails(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert trail tuples, refreshing existing rows in place. Returns rows attempted.

    Each tuple is
    (id, name, kind, source, url, min_lat, min_lng, max_lat, max_lng, center_lat, center_lng,
    geojson).
    """
    columns: tuple[LiteralString, ...] = (
        "id",
        "name",
        "kind",
        "source",
        "url",
        "min_lat",
        "min_lng",
        "max_lat",
        "max_lng",
        "center_lat",
        "center_lng",
        "geojson",
    )
    return upsert_rows(con, "trails", columns, rows)


_FIRE_COLUMNS: tuple[LiteralString, ...] = (
    "id",
    "source_key",
    "feature_id",
    "irwin_id",
    "name",
    "status",
    "fire_year",
    "discovery_date",
    "percent_contained",
    "gis_acres",
    "incident_url",
    "is_point",
    "min_lat",
    "min_lng",
    "max_lat",
    "max_lng",
    "center_lat",
    "center_lng",
    "geojson",
    "fetched_at",
)


def upsert_fire_perimeters(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert fire perimeter / point rows (issue #227), refreshing existing rows in place.

    Tuple order is :data:`_FIRE_COLUMNS`. The MTBS severity columns are written separately by
    :func:`apply_fire_severity` (backfill-style), so a plain refresh never blanks them."""
    return upsert_rows(con, "fire_perimeters", _FIRE_COLUMNS, rows)


def replace_fire_lane(con: psycopg.Connection, source_key: str, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert ``rows`` for ``source_key`` and delete any existing row in that lane no longer
    present in the source (issue #227's active lane - "a contained fire drops out").

    Wrapped in one transaction so a reader never sees the lane mid-swap. Returns rows upserted.
    An empty ``rows`` with no prior data is a no-op; an empty ``rows`` after the source
    legitimately reports zero active fires clears the lane."""
    keep_ids = [row[0] for row in rows]
    with con.transaction():
        if keep_ids:
            con.execute(
                "DELETE FROM fire_perimeters WHERE source_key = %s AND id <> ALL(%s)",
                [source_key, keep_ids],
            )
        else:
            con.execute("DELETE FROM fire_perimeters WHERE source_key = %s", [source_key])
        upsert_fire_perimeters(con, rows)
    return len(rows)


_MTBS_UPDATE: dict[str, LiteralString] = {
    "irwin_id": (
        "UPDATE fire_perimeters SET severity_unburned_acres = %s, severity_low_acres = %s, "
        "severity_moderate_acres = %s, severity_high_acres = %s, dominant_severity = %s, "
        "mtbs_fire_id = COALESCE(%s, mtbs_fire_id) WHERE irwin_id = %s"
    ),
    "mtbs_fire_id": (
        "UPDATE fire_perimeters SET severity_unburned_acres = %s, severity_low_acres = %s, "
        "severity_moderate_acres = %s, severity_high_acres = %s, dominant_severity = %s, "
        "mtbs_fire_id = COALESCE(%s, mtbs_fire_id) WHERE mtbs_fire_id = %s"
    ),
}


def apply_fire_severity(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Write MTBS burn-severity enrichment onto existing fire rows (issue #227), matched by
    ``irwin_id`` or ``mtbs_fire_id``. Tuple:
    ``(match_key, match_value, unburned, low, moderate, high, dominant, mtbs_fire_id)`` where
    ``match_key`` is ``"irwin_id"`` or ``"mtbs_fire_id"``. Returns the total number of perimeter
    rows updated (a single MTBS row can match more than one, e.g. the same ``irwin_id`` in both
    the active and history lanes during the brief overlap window)."""
    updated = 0
    for match_key, match_value, unburned, low, moderate, high, dominant, mtbs_fire_id in rows:
        if match_key not in _MTBS_UPDATE:
            raise ValueError(f"bad MTBS match key: {match_key!r}")
        if match_value in (None, ""):
            continue
        result = con.execute(
            _MTBS_UPDATE[match_key],
            [unburned, low, moderate, high, dominant, mtbs_fire_id, match_value],
        )
        updated += result.rowcount or 0
    return updated


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


def load_region_place(con: psycopg.Connection, region_id: str) -> tuple[bool, str | None]:
    """Cached place name for a region, if already resolved. ``(found, place_name)`` -
    ``found=False`` means no lookup has been attempted yet (caller should resolve and save);
    ``found=True, place_name=None`` means a lookup ran and found nothing notable nearby."""
    row = con.execute("SELECT place_name FROM region_places WHERE region_id = %s", [region_id]).fetchone()
    if row is None:
        return False, None
    return True, row[0]


def save_region_place(con: psycopg.Connection, region_id: str, place_name: str | None) -> None:
    con.execute(
        "INSERT INTO region_places (region_id, place_name) VALUES (%s, %s) ON CONFLICT (region_id) DO NOTHING",
        [region_id, place_name],
    )


# Half-width of the bounding box `observations_missing_elevation(near=...)` restricts to: ~1600
# km, comfortably past the home radius and any corridor trip, but small enough that the query
# range-scans ix_observations_lat_lng instead of sorting the whole backlog.
_NEAR_WINDOW_DEG = 15.0


def observations_missing_elevation(
    con: psycopg.Connection, limit: int, *, near: tuple[float, float] | None = None
) -> list[tuple[int, float, float]]:
    """Up to ``limit`` research-grade observations with in-range coordinates but no elevation
    yet (issue #36). Non-research-grade rows are skipped - scoring only ever reads research-grade
    (scoring._BINNED), so enriching the rest would just burn Open-Meteo quota. Obscured rows are
    skipped too - their cached point is iNat's randomized decoy, so its elevation would be
    meaningless. Out-of-range lat/lng is excluded here so one bad row can't sit at the head of
    the queue and wedge the backfill (``elevation.lookup_batch`` would raise on it every run).

    ``near`` (a ``(lat, lng)``) restricts the queue to a bounding box around that point and
    orders it by squared planar distance, so a Refresh drains the cells the visitor is actually
    looking at first instead of the oldest-id rows scattered nationwide. The box (a) keeps
    Postgres range-scanning ``ix_observations_lat_lng`` instead of sorting the whole
    missing-elevation backlog on every call, and (b) is wide enough (`_NEAR_WINDOW_DEG`, ~1600
    km) to cover the home radius and any plausible corridor trip - rows further out aren't on
    the visitor's cards anyway, and the whole-backlog drain (the hourly prod cron) passes no
    ``near`` and stays ``ORDER BY id``. The degree-space distance is a cheap proxy: it only
    decides ordering within the box, and iNat fungal data is effectively all mid-latitude. A box
    that straddles the antimeridian is split into its two wrapped longitude ranges."""
    box_sql: LiteralString = ""
    box_params: list[float] = []
    if near is not None:
        plat, plng = near
        lo_lng, hi_lng = plng - _NEAR_WINDOW_DEG, plng + _NEAR_WINDOW_DEG
        box_params = [plat - _NEAR_WINDOW_DEG, plat + _NEAR_WINDOW_DEG]
        if lo_lng < -180.0 or hi_lng > 180.0:
            # The box straddles the antimeridian (a visitor in the western Aleutians). Split the
            # longitude test into its two wrapped ranges so Postgres still range-scans
            # ix_observations_lat_lng instead of matching nothing on the out-of-range bound.
            box_sql = " AND lat BETWEEN %s AND %s AND (lng BETWEEN %s AND 180 OR lng BETWEEN -180 AND %s) "
            box_params += [(lo_lng + 180.0) % 360.0 - 180.0, (hi_lng + 180.0) % 360.0 - 180.0]
        else:
            box_sql = " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s "
            box_params += [lo_lng, hi_lng]
        # Wrap the longitude delta too, so a point just across the dateline sorts as near, not
        # ~360 deg away.
        order_sql: LiteralString = (
            "ORDER BY (lat - %s) * (lat - %s) + power(LEAST(ABS(lng - %s), 360.0 - ABS(lng - %s)), 2)"
        )
        order_params: list[float] = [plat, plat, plng, plng]
    else:
        order_sql = "ORDER BY id"
        order_params = []
    rows = con.execute(
        """
        SELECT id, lat, lng FROM observations
        WHERE elevation_m IS NULL
              AND lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180
              AND quality_grade = 'research'
              AND NOT COALESCE(obscured, false)
        """
        + box_sql
        + order_sql
        + " LIMIT %s",
        [*box_params, *order_params, limit],
    ).fetchall()
    return [(int(obs_id), float(lat), float(lng)) for obs_id, lat, lng in rows]


def set_observation_elevations(con: psycopg.Connection, rows: Sequence[tuple[int, int]]) -> int:
    """Write looked-up elevations back. ``rows`` is ``(observation_id, elevation_m)`` and carries
    only rows that got a real value - a point Open-Meteo had no answer for is left NULL and
    retried on the next backfill (a healthy response always has a value, even 0 for open sea,
    so this does not loop in practice)."""
    if not rows:
        return 0
    with con.cursor() as cur:
        cur.executemany(
            "UPDATE observations SET elevation_m = %s WHERE id = %s", [(elev, obs_id) for obs_id, elev in rows]
        )
    return len(rows)


# --- Precipitation cache (issue #226) -------------------------------------------------------


def cached_precip(
    con: psycopg.Connection, cell_id: str, start: dt.date, end: dt.date, *, source: str | None = None
) -> dict[dt.date, float | None]:
    """``date -> precip_mm`` already cached for ``cell_id`` in ``[start, end]`` (inclusive).

    A day absent from the result was never fetched; a day present with value ``None`` is one
    Open-Meteo returned null for (ERA5 lag). ``backfill_precip`` treats both the same - "not
    known yet", so it refetches the span - and never records a window sum that touches such a
    day. ``source`` restricts to one origin: the per-observation backfill trusts only ERA5
    archive rows, never the provisional forecast rows the layer refresh also writes."""
    query: LiteralString = "SELECT date, precip_mm FROM precip_daily WHERE cell_id = %s AND date BETWEEN %s AND %s"
    params: list[Any] = [cell_id, start, end]
    if source is not None:
        query += " AND source = %s"
        params.append(source)
    rows = con.execute(query, params).fetchall()
    return {day: (float(mm) if mm is not None else None) for day, mm in rows}


def upsert_precip_days(con: psycopg.Connection, cell_id: str, days: Mapping[dt.date, float | None], source: str) -> int:
    """Cache per-day precipitation for one grid cell. Overwrites an existing ``(cell_id, date)``
    row so a later archive pull can replace a forecast estimate (or fill a previously-null day).
    Returns rows written."""
    if not days:
        return 0
    now = dt.datetime.now(dt.UTC)
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO precip_daily (cell_id, date, precip_mm, source, fetched_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (cell_id, date) DO UPDATE SET
                precip_mm = EXCLUDED.precip_mm, source = EXCLUDED.source, fetched_at = EXCLUDED.fetched_at
            """,
            [(cell_id, day, mm, source, now) for day, mm in days.items()],
        )
    return len(days)


def observations_missing_precip(
    con: psycopg.Connection, limit: int, *, near: tuple[float, float] | None = None
) -> list[tuple[int, float, float, dt.date]]:
    """Up to ``limit`` research-grade, non-obscured observations with coordinates and an
    ``observed_on`` but at least one of ``precip_7d_mm`` / ``precip_30d_mm`` still unset
    (issue #226). Same research-grade/obscured/in-range filters as
    :func:`observations_missing_elevation`; ordered by ``near`` (planar distance) when given,
    else oldest ``observed_on`` first so a cell's history fills in order.

    ``precip_7d_mm IS NULL OR precip_30d_mm IS NULL`` is the pending sentinel: a row whose 7 d
    sum lands but whose 30 d sum still touches an ERA5-null day is written partially (7 d only)
    and reappears here next pass so the 30 d column gets retried too.

    Observations dated before ERA5's coverage (1940) are excluded - Open-Meteo's archive has no
    data for them and, left in, one such row 400s the archive request for its whole grid cell
    and wedges the backfill (same reasoning as the lat/lng-range filter)."""
    box_sql: LiteralString = ""
    params: list[Any] = []
    order_sql: LiteralString = "ORDER BY observed_on"
    if near is not None:
        plat, plng = near
        box_sql = " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s "
        params += [plat - _NEAR_WINDOW_DEG, plat + _NEAR_WINDOW_DEG, plng - _NEAR_WINDOW_DEG, plng + _NEAR_WINDOW_DEG]
        order_sql = "ORDER BY (lat - %s) * (lat - %s) + (lng - %s) * (lng - %s)"
        params += [plat, plat, plng, plng]
    rows = con.execute(
        """
        SELECT id, lat, lng, observed_on FROM observations
        WHERE (precip_7d_mm IS NULL OR precip_30d_mm IS NULL)
              AND observed_on >= DATE '1940-02-01'
              AND lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180
              AND quality_grade = 'research'
              AND NOT COALESCE(obscured, false)
        """
        + box_sql
        + order_sql
        + " LIMIT %s",
        [*params, limit],
    ).fetchall()
    return [(int(obs_id), float(lat), float(lng), observed_on) for obs_id, lat, lng, observed_on in rows]


def set_observation_precip(con: psycopg.Connection, rows: Sequence[tuple[int, float | None, float | None]]) -> int:
    """Write ``(observation_id, precip_7d_mm, precip_30d_mm)`` back. Callers pass only rows whose
    7 d window was fully covered - a row still missing an ERA5 day is left NULL (pending)."""
    if not rows:
        return 0
    with con.cursor() as cur:
        cur.executemany(
            "UPDATE observations SET precip_7d_mm = %s, precip_30d_mm = %s WHERE id = %s",
            [(mm7, mm30, obs_id) for obs_id, mm7, mm30 in rows],
        )
    return len(rows)


def stale_precip_region_ids(con: psycopg.Connection, older_than_hours: float) -> list[str]:
    """Active region cells whose ``precipitation`` row is missing or older than
    ``older_than_hours`` (issue #226 Part 2). Lets the layer refresh skip cells done recently, so
    a re-run - or one resumed after the scheduler restarted mid-pass - continues instead of
    starting from scratch. Empty when ``regions`` doesn't exist yet."""
    try:
        rows = con.execute(
            """
            SELECT r.region_id
            FROM regions r
            LEFT JOIN precipitation p ON p.region_id = r.region_id
            WHERE p.region_id IS NULL
               OR p.updated_at IS NULL
               OR p.updated_at < now() - make_interval(hours => %s)
            """,
            [older_than_hours],
        ).fetchall()
    except psycopg.errors.UndefinedTable:
        con.rollback()
        return []
    return [region_id for (region_id,) in rows]


def region_precip(con: psycopg.Connection, region_ids: Collection[str]) -> dict[str, dict[str, float | None]]:
    """``region_id -> {"precip_7d_mm", "precip_14d_mm", "precip_30d_mm"}`` from the
    ``precipitation`` layer table (issue #226). Absent when that cell has never been refreshed."""
    if not region_ids:
        return {}
    try:
        rows = con.execute(
            "SELECT region_id, precip_7d_mm, precip_14d_mm, precip_30d_mm FROM precipitation WHERE region_id = ANY(%s)",
            [list(region_ids)],
        ).fetchall()
    except psycopg.errors.UndefinedTable:
        con.rollback()
        return {}
    return {
        region_id: {"precip_7d_mm": mm7, "precip_14d_mm": mm14, "precip_30d_mm": mm30}
        for region_id, mm7, mm14, mm30 in rows
    }


def upsert_region_precip(
    con: psycopg.Connection, rows: Sequence[tuple[str, float | None, float | None, float | None]]
) -> int:
    """Upsert ``(region_id, precip_7d_mm, precip_14d_mm, precip_30d_mm)`` into ``precipitation``."""
    if not rows:
        return 0
    now = dt.datetime.now(dt.UTC)
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO precipitation (region_id, precip_7d_mm, precip_14d_mm, precip_30d_mm, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (region_id) DO UPDATE SET
                precip_7d_mm = EXCLUDED.precip_7d_mm, precip_14d_mm = EXCLUDED.precip_14d_mm,
                precip_30d_mm = EXCLUDED.precip_30d_mm, updated_at = EXCLUDED.updated_at
            """,
            [(region_id, mm7, mm14, mm30, now) for region_id, mm7, mm14, mm30 in rows],
        )
    return len(rows)


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


def delete_location(con: psycopg.Connection, device_id: str) -> None:
    """Issue #81: let a visitor delete their saved "Set location" override outright."""
    con.execute("DELETE FROM app_location WHERE device_id = %s", [device_id])


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
