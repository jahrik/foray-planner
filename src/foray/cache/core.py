"""Postgres schema, migrations, and generic upsert primitives - the foundation the
rest of the ``foray.cache`` package builds on. ``connect``/``apply_schema`` are the
entrypoints every CLI/API/cron path calls; ``upsert_rows``/``copy_upsert``/
``copy_insert_ignore`` are the shared write primitives every domain module uses.

Observations are keyed by iNat id, so re-ingesting the same window is a no-op
(``ON CONFLICT DO NOTHING``). Region binning (grid cell) is derived in SQL from
lat/lng and ``h3_resolution`` so it is never stored redundantly.

Connections are opened with ``autocommit=True`` (nothing here was written against explicit
transactions) - callers that need atomicity across statements (e.g.
``regions.build_phenology``'s drop+rebuild) wrap them in an explicit
``with con.transaction():`` block instead.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, LiteralString

import psycopg

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
    url         TEXT,
    reservable  BOOLEAN,             -- RIDB `Reservable` (needs full=true); NULL for OSM / unknown
    fee_low     DOUBLE PRECISION,    -- nightly USD parsed from the fee prose (issue #306), low/high
    fee_high    DOUBLE PRECISION     -- of the plausible amounts named; NULL when none parse
);

-- Public-land ownership polygons (BLM Surface Management Agency + USFS admin forest
-- boundaries, via ArcGIS REST). Keyed by "{source}:{source_id}" so re-ingesting the same
-- area is a no-op. Geometry is stored as GeoJSON *text*; the `geom geography` column
-- (PostGIS Phase 0, migration 18) and its GIST index serve the "land near here" query. The
-- geom BEFORE trigger derives geom from geojson. Informational only: this shows ownership
-- and links the official source; it never asserts camping legality (see AGENTS.md).
CREATE TABLE IF NOT EXISTS public_land (
    id          TEXT PRIMARY KEY,    -- "{source}:{source_id}", e.g. "usfs:1234"
    agency      TEXT,                -- "BLM", "USFS"
    unit        TEXT,                -- unit / forest name when the source provides one
    source      TEXT,                -- "blm", "usfs"
    url         TEXT,                -- official source (the ArcGIS service)
    geojson     TEXT                 -- polygon geometry as GeoJSON text
    -- area_deg2 added by migration 49, issue #335 PR 2 follow-up: planar area of `geom`,
    -- computed once per polygon write (trg_public_land_geom_area) instead of recomputed from
    -- scratch on every trail<->land lookup - see that migration's comment for why.
);

-- Trails (OSM Overpass): hiking paths, named hiking routes, and trailheads. Keyed by
-- "{source}:{osm_type}/{osm_id}" so re-ingesting the same area is a no-op. The `geom geography`
-- column (PostGIS Phase 0, migration 17) + its GIST index serve "trails near here" and the exact
-- point-to-trail distance. `center_lat`/`center_lng` stay as a cheap representative point for
-- callers that want one without parsing the geometry. Informational only: links the OSM source;
-- makes no legal-access claim (see AGENTS.md).
--
-- `geojson` (the actual GeoJSON text - LineString/MultiLineString for paths/routes, Point for
-- trailheads) moved to a separate `trail_geometry(id, geojson)` table (migration 45, issue #333
-- PR 2): `queries.trails_near`'s relevance/longest sort fetches up to 500 candidates before
-- trimming to the card list, and most of those never render geometry - `geom` (small, needed by
-- every spatial predicate) stays here; `geojson` (large, only needed when a caller actually asks
-- for it) lives there, joined in on demand. `trail_geometry`'s `AFTER` trigger keeps `geom` here
-- in sync from there (see `foray_trail_geom_from_geometry`); `cache.upsert_trails` upserts this
-- table first so that trigger's `UPDATE trails ...` always finds an existing row.
CREATE TABLE IF NOT EXISTS trails (
    id          TEXT PRIMARY KEY,    -- "{source}:{osm_type}/{osm_id}", e.g. "osm:way/42"
    name        TEXT,
    kind        TEXT,                -- "path" (way) | "route" (relation) | "trailhead" (node)
    source      TEXT,                -- "osm"
    url         TEXT,                -- official source (the OSM element page)
    center_lat  DOUBLE PRECISION,    -- representative point on the trail
    center_lng  DOUBLE PRECISION,
    geojson     TEXT,                -- historical only - moved to `trail_geometry` by migration
                                     -- 45; column dropped there too. Left in this base SCHEMA
                                     -- (never read after that migration runs) rather than
                                     -- rewritten, matching how this file treats SCHEMA as frozen
                                     -- history and _MIGRATIONS as the source of truth.
    connects    TEXT[],              -- trailhead rows only: ids of the path/route trails whose
                                     -- geometry passes within ~35 m of the node, computed at
                                     -- ingest so selecting a trailhead draws its trail straight
                                     -- from cache with no live Overpass call (issue #306)
    length_km   DOUBLE PRECISION,    -- path/route rows: great-circle length of the full polyline
    attrs       TEXT,                -- path/route rows: JSON of the OSM detail tags (surface,
                                     -- sac_scale, trail_visibility, network, operator, informal),
                                     -- stored as text like `geojson` - the map/Details view reads it
    forage_obs    INTEGER,           -- count of research-grade, non-obscured fungi observations
                                     -- within _FORAGE_OBS_RADIUS_M of the line - a genus-agnostic
                                     -- "how much fruits along here" signal the map ramps + the
                                     -- card shows. Refreshed in rotation by `backfill_forage_obs`.
    forage_obs_at TIMESTAMPTZ        -- when forage_obs was last computed (NULLs go first in the
                                     -- backfill rotation); staleness is fine, it drifts slowly
    -- land_agency / land_unit added by migration 48, issue #335 PR 2: the public-land unit a
    -- trail's representative point falls inside, persisted at ingest instead of recomputed via a
    -- live point-in-polygon join on every request (see queries.trails_near / get_trail history).
);

-- Wildfire perimeters + points (issue #227). An active fire and a recent burn scar are the
-- same polygon at different life stages, so one table holds both, split by `source_key` into
-- two refresh lanes that never clobber each other:
--   'wfigs_active'      status='active'      - fast cadence, REPLACE semantics (rows gone from
--                                              the source each refresh are deleted, not kept)
--   'perimeter_history' status='historical'  - slow cadence, plain upsert; last 3 completed
--                                              fire years + current (the morel productivity curve)
-- Geometry is GeoJSON *text* + a representative center, same as public_land/trails; the
-- `geom geography` column (PostGIS Phase 0, migration 19) + its GIST index serve "fire near
-- here". Informational only: links the official incident page, never asserts a road/forest
-- closure (see AGENTS.md). MTBS severity columns stay NULL until MTBS publishes (~1.5-2 yr
-- after the season); the layer works without them.
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
    center_lat               DOUBLE PRECISION,
    center_lng               DOUBLE PRECISION,
    geojson                  TEXT,
    fetched_at               TIMESTAMPTZ
);

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
-- region forever - regions are a fixed grid (scoring.py's h3_resolution binning), so a region's
-- centroid never moves and its notable place name never needs re-resolving. `place_name` is
-- nullable on purpose: a row existing means "already looked up", regardless of whether a
-- notable place was found - so a remote/rural region with no notable place nearby is cached
-- as a negative result instead of re-hitting Nominatim on every destinations refresh.
CREATE TABLE IF NOT EXISTS region_places (
    region_id  TEXT PRIMARY KEY,
    place_name TEXT
);

-- Selected-destination satellite fill (#293 follow-up): caches one Esri export pair (aerial
-- photo + its transparent roads/labels overlay, both JPEG/PNG bytes straight from the source)
-- per grid region forever - same "regions are a fixed grid, never re-resolve" reasoning as
-- region_places above. A live Esri export at the resolution the frontend wants (4096px) takes
-- 25-45s server-side, which is fine paid once at backfill time (`foray backfill-satellite`) but
-- not something a page load should ever block on - see sources/satellite.py.
-- image/labels are nullable (issue #334 PR 1): when `Settings.spaces` is configured, the
-- rasters live in the DO Space instead and only image_url/labels_url are populated - see
-- cache.save_region_satellite. Unconfigured (the default) keeps storing bytes here directly.
CREATE TABLE IF NOT EXISTS region_satellite (
    region_id  TEXT PRIMARY KEY,
    image      BYTEA,                 -- World_Imagery export, JPEG bytes (Postgres fallback)
    labels     BYTEA,                 -- Boundaries_and_Places export, transparent PNG (fallback)
    image_url  TEXT,                  -- public Space URL, when Settings.spaces is configured
    labels_url TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
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

-- trails_near / camps_near now filter on the PostGIS `geom` GIST index (ix_trails_geom /
-- ix_campsites_geom, built in apply_schema's CONCURRENTLY block); the old bbox btree
-- (ix_trails_bbox) and its four columns were dropped by migrations 23-27.
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
SCHEMA_VERSION = 7

# Fixed advisory-lock key so two processes starting together (API + scheduler) serialize on
# the full apply_schema path instead of racing CREATE INDEX CONCURRENTLY.
_SCHEMA_LOCK_KEY = 4915623

# CONCURRENTLY-built indexes, applied outside any transaction (autocommit) so they never hold a
# write lock on the ~1.9M-row observations table during a rolling deploy. Each is attempted
# independently and a failure is logged, not raised: IF NOT EXISTS / IF EXISTS makes them
# idempotent, but two instances starting together can still race (one loses), and none of these
# is a correctness dependency - only a query-speed optimization.
#
# Most entries are a bare statement. A `DROP` that retires an old index in favor of one built
# earlier in this same list is instead a (statement, "guard-name") pair: the DROP only runs if
# the CREATE tagged with that same guard-name succeeded, so a failed/cancelled build never
# leaves the table with neither index (see apply_schema's loop below).
_CONCURRENT_INDEXES: list[LiteralString | tuple[LiteralString, str]] = [
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_observations_revalidated_at ON observations (revalidated_at)",
    # Supersedes the old non-partial ix_observations_taxon_observed: BINNED always filters
    # quality_grade = 'research' first, so the partial index is smaller and better matched.
    # Create the replacement first, drop the old one only after - a failed/cancelled build must
    # not leave the table with neither index.
    (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_observations_taxon_observed_research "
        "ON observations (taxon_id, observed_on) WHERE quality_grade = 'research'",
        "taxon_observed_research",
    ),
    ("DROP INDEX CONCURRENTLY IF EXISTS ix_observations_taxon_observed", "taxon_observed_research"),
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
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_public_land_geom ON public_land USING GIST (geom)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_fire_perimeters_geom ON fire_perimeters USING GIST (geom)",
    # issue #333: partial - `geom` is nullable (populated by the BEFORE trigger, migrations
    # 20/21/22) and unset until a row's first insert/update touches it, so a plain index over
    # the whole ~1.9M/~1.2M-row tables wastes space entering NULLs no spatial query ever
    # matches. Same two-step swap as ix_observations_taxon_observed_research above: build the
    # partial replacement first, drop the old one only once it's live - a failed/cancelled
    # build must never leave the table with neither index.
    (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_observations_geom_notnull "
        "ON observations USING GIST (geom) WHERE geom IS NOT NULL",
        "observations_geom_notnull",
    ),
    ("DROP INDEX CONCURRENTLY IF EXISTS ix_observations_geom", "observations_geom_notnull"),
    (
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_trails_geom_notnull ON trails USING GIST (geom) "
        "WHERE geom IS NOT NULL",
        "trails_geom_notnull",
    ),
    ("DROP INDEX CONCURRENTLY IF EXISTS ix_trails_geom", "trails_geom_notnull"),
    # issue #335 PR 2 follow-up: `backfill_trail_land`'s polygon-driven pass needs to find, for
    # one `public_land` polygon at a time, every trail whose representative point falls inside
    # it - without this expression index that's a seq scan of the whole trails table per polygon
    # (confirmed live: an `EXPLAIN ANALYZE` without it showed a `Seq Scan on trails` per polygon,
    # ~2.1M rows visited 11,757 times). A dedicated index rather than reusing
    # `ix_trails_geom_notnull`: this table's `geom` is the trail's actual line/point geometry, not
    # `(center_lng, center_lat)` - the two disagree for a path/route/road row (see the "accepted
    # imprecision" note on `_TRAIL_LAND_JOIN`'s callers), and this feature has always kept that
    # specific, deliberate "representative point" semantic. Indexed on the `::geography` cast, not
    # bare `geometry` - the query's `ST_DWithin` call casts to geography (matching
    # `_TRAIL_LAND_JOIN` everywhere else), and Postgres will only use an expression index when the
    # indexed expression appears verbatim in the query; a first attempt at this index (geometry,
    # uncast) silently went unused for exactly that reason and the seq scan above went unnoticed
    # until a live `EXPLAIN ANALYZE` caught it.
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_trails_center_point_geog ON trails "
    "USING GIST ((ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography))",
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
    # issue #117: `year` was write-only (populated at ingest, upserted at resync,
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
    # --- PostGIS cleanup (issue #268 PR 5) -------------------------------------------------
    # PostGIS Phase 1 switched every "near" read to the `geom` GIST index, so the bbox btree
    # and the four min/max columns feeding it are dead. `geojson` stays (the geom trigger's
    # raw input). Irreversible - snapshot prod before this ships. Rolling-deploy safe: by the
    # time this lands every instance is on the PR-3+ code that selects neither column.
    (23, "DROP INDEX IF EXISTS ix_trails_bbox"),
    (24, "DROP INDEX IF EXISTS ix_fire_perimeters_bbox"),
    (
        25,
        "ALTER TABLE trails DROP COLUMN IF EXISTS min_lat, DROP COLUMN IF EXISTS min_lng, "
        "DROP COLUMN IF EXISTS max_lat, DROP COLUMN IF EXISTS max_lng",
    ),
    (
        26,
        "ALTER TABLE public_land DROP COLUMN IF EXISTS min_lat, DROP COLUMN IF EXISTS min_lng, "
        "DROP COLUMN IF EXISTS max_lat, DROP COLUMN IF EXISTS max_lng",
    ),
    (
        27,
        "ALTER TABLE fire_perimeters DROP COLUMN IF EXISTS min_lat, DROP COLUMN IF EXISTS min_lng, "
        "DROP COLUMN IF EXISTS max_lat, DROP COLUMN IF EXISTS max_lng",
    ),
    # --- Trail preload (issue #306) ------------------------------------------------------
    # `connects` on a trailhead row lists the trail ids its geometry touches, computed at
    # ingest (trails._parse_trails) so `resolve_trail_network` draws the trail from cache
    # instead of a live per-selection Overpass query. Additive - older images ignore it.
    (28, "ALTER TABLE trails ADD COLUMN IF NOT EXISTS connects TEXT[]"),
    # --- Trail attributes (issue #306) ---------------------------------------------------
    # `length_km` (great-circle sum over the full pre-thinned polyline) drives the "hide the
    # 50 m stub" list filter and the "longest" sort; `attrs` is JSON text of the OSM detail
    # tags (surface, sac_scale, trail_visibility, network, operator, informal) the Details
    # view and relevance score read - stored as text like `geojson`. Both additive.
    (29, "ALTER TABLE trails ADD COLUMN IF NOT EXISTS length_km DOUBLE PRECISION"),
    (30, "ALTER TABLE trails ADD COLUMN IF NOT EXISTS attrs TEXT"),
    # --- Campground attributes (issue #306) --------------------------------------------
    # `reservable` from RIDB's full=true listing; `fee_low`/`fee_high` parsed from the fee
    # prose so the "free camp" filter and a fee range are trustworthy. All additive.
    (31, "ALTER TABLE campsites ADD COLUMN IF NOT EXISTS reservable BOOLEAN"),
    (32, "ALTER TABLE campsites ADD COLUMN IF NOT EXISTS fee_low DOUBLE PRECISION"),
    (33, "ALTER TABLE campsites ADD COLUMN IF NOT EXISTS fee_high DOUBLE PRECISION"),
    # --- Per-trail foraging density -----------------------------------------------------
    # `forage_obs` counts research-grade, non-obscured fungi observations hugging the trail
    # line (genus-agnostic) so the map can ramp a line by how productive the ground is and the
    # card can show it. `forage_obs_at` orders the backfill rotation (`backfill_forage_obs`).
    # Both additive; NULL until the first backfill pass reaches the row.
    (34, "ALTER TABLE trails ADD COLUMN IF NOT EXISTS forage_obs INTEGER"),
    (35, "ALTER TABLE trails ADD COLUMN IF NOT EXISTS forage_obs_at TIMESTAMPTZ"),
    # issue #332: one row per scheduled-job run (the `foray job` wrapper - see foray.jobs),
    # so a run's outcome is queryable instead of living only in a cron container's stdout.
    # `status` is "ok" / "error" / "skipped" (advisory-lock overlap - see jobs.run).
    (
        36,
        "CREATE TABLE IF NOT EXISTS job_runs ("
        "id BIGSERIAL PRIMARY KEY, job TEXT NOT NULL, started_at TIMESTAMPTZ NOT NULL, "
        "ended_at TIMESTAMPTZ, status TEXT NOT NULL, rows INTEGER, duration_ms INTEGER, "
        "http_429_count INTEGER)",
    ),
    # /healthz/data and any "last run of X" query filter by job then want the newest rows
    # first - matches how ix_observations_taxon_observed_research etc. are shaped for their
    # own hot query.
    (37, "CREATE INDEX IF NOT EXISTS ix_job_runs_job_started ON job_runs (job, started_at DESC)"),
    # issue #333: leaves 10% free space per page on the two tables that take the heaviest
    # UPDATE traffic (resync ~2000 obs/hr, forage backfill 20k trails/6h) so an UPDATE that
    # doesn't grow the row can reuse space on the same page (HOT update) instead of always
    # forcing a new page - only affects pages written after this runs, not existing ones. A
    # one-time `pg_repack` rewrites what's already there; not automated (touches prod directly).
    (38, "ALTER TABLE observations SET (fillfactor = 90)"),
    (39, "ALTER TABLE trails SET (fillfactor = 90)"),
    # issue #333: session defaults for the connecting role, applied via ALTER ROLE CURRENT_USER
    # so this works unchanged whether that role is local dev's "foray" or prod's DO-managed
    # admin user - neither needs superuser to set its own defaults. random_page_cost=4 is the
    # spinning-disk-era default and pushes the planner away from index scans it should be
    # taking on SSD-backed storage (local disk and DO's managed volumes both qualify);
    # effective_io_concurrency mirrors that for prefetch. effective_cache_size is deliberately
    # NOT set here - it should reflect the actual plan's RAM, which this migration can't know,
    # and DO may already auto-size it; check `SHOW effective_cache_size` on the real instance
    # before setting that one by hand. statement_timeout is also deliberately excluded - it
    # needs to apply to the API's pooled connections only, not the same role's cron/migration
    # runs, which legitimately take minutes (see api/deps.py instead).
    (40, "ALTER ROLE CURRENT_USER SET random_page_cost = 1.1"),
    (41, "ALTER ROLE CURRENT_USER SET effective_io_concurrency = 200"),
    # issue #333: observations (resync ~2000 rows/hr) and trails (forage backfill 20k/6h) take
    # steady UPDATE traffic a table-size-percentage default doesn't suit well once a table is
    # large - a 2% threshold on a multi-million-row table is a lot of dead tuples before
    # autovacuum fires. Raising the cost limit lets it work through that backlog faster once
    # triggered instead of throttling itself against foreground query I/O indefinitely.
    (
        42,
        "ALTER TABLE observations SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_vacuum_cost_limit = 2000)",
    ),
    (
        43,
        "ALTER TABLE trails SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_vacuum_cost_limit = 2000)",
    ),
    # issue #333 PR 2: lets scoring.regions._build_phenology_locked warm shared_buffers with the
    # fresh `phenology`/`regions` tables right after the swap - a bundled contrib extension, but
    # not guaranteed superuser-installable on every box (DO's managed PG allowlists it, a local
    # dev/CI Postgres image might not have the .so at all). Wrapped in a DO block that swallows
    # any error (Copilot review, PR #348: a plain `CREATE EXTENSION` failing here - undefined
    # file, insufficient privilege - would abort `apply_schema()` itself and the box couldn't
    # start at all, which defeats the whole point of the call-site try/except around
    # `pg_prewarm()` in regions.py; that one still degrades gracefully on `UndefinedFunction`
    # for the *rarer* case of this migration itself never having run yet on an old deploy).
    (
        44,
        """
        DO $$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS pg_prewarm;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'pg_prewarm extension unavailable, skipping: %', SQLERRM;
        END $$;
        """,
    ),
    # issue #333 PR 2: split the (large, avg ~485 B, 550 MB total on this box's 1.19M trail rows)
    # `geojson` TEXT payload off `trails` into its own 1:1 table. `queries.trails_near`'s
    # `relevance`/`longest` sort fetches up to 500 candidate rows before trimming to the card
    # list (`with_geometry=False` already drops it there for exactly this reason) - every other
    # column on `trails` stays put, so a plain column scan of the "hundreds of candidates" path
    # no longer drags the biggest column on the table along for rows that never render geometry.
    # `geom` (the compact PostGIS geography every spatial trails query filters/sorts on) stays on
    # `trails` untouched - this migration doesn't touch it, so every existing row's already-valid
    # `geom` survives as-is; only future writes go through the new trigger below.
    (
        45,
        """
        CREATE TABLE IF NOT EXISTS trail_geometry (
            id      TEXT PRIMARY KEY REFERENCES trails (id) ON DELETE CASCADE,
            geojson TEXT
        );
        INSERT INTO trail_geometry (id, geojson)
            SELECT id, geojson FROM trails
            ON CONFLICT (id) DO NOTHING;
        DROP TRIGGER IF EXISTS trg_trails_geom ON trails;
        CREATE OR REPLACE FUNCTION foray_trail_geom_from_geometry() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            new_geom geography;
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.geojson IS NOT DISTINCT FROM OLD.geojson THEN
                RETURN NEW;
            END IF;
            IF NEW.geojson IS NULL THEN
                new_geom := NULL;
            ELSE
                BEGIN
                    new_geom := ST_MakeValid(ST_GeomFromGeoJSON(NEW.geojson))::geography;
                EXCEPTION WHEN others THEN
                    new_geom := NULL;
                    RAISE WARNING 'foray_trail_geom_from_geometry: bad geometry for trails.%: %',
                        NEW.id, SQLERRM;
                END;
            END IF;
            -- Cross-table side effect (trail_geometry -> trails.geom), not a same-row NEW
            -- assignment like foray_geom_from_geojson() - geom and geojson now live on
            -- different tables, so this can't be a plain BEFORE-trigger column derivation.
            -- Requires the trails row to already exist (cache.upsert_trails upserts trails
            -- before trail_geometry for exactly this reason).
            UPDATE trails SET geom = new_geom WHERE id = NEW.id;
            RETURN NEW;
        END;
        $$;
        CREATE OR REPLACE TRIGGER trg_trail_geometry_geom AFTER INSERT OR UPDATE ON trail_geometry
            FOR EACH ROW EXECUTE FUNCTION foray_trail_geom_from_geometry();
        ALTER TABLE trails DROP COLUMN IF EXISTS geojson;
        """,
    ),
    # issue #334 PR 1: region_satellite's rasters move to a DO Space when Settings.spaces is
    # configured (cache.save_region_satellite) - existing bytea columns become optional
    # fallback storage instead of the only storage, and a url column holds the Space pointer.
    # No data migration here: an already-cached region's bytea rows keep serving from Postgres
    # as before until the next `backfill-satellite --refresh` re-fetches it through whichever
    # path is configured - forcing every existing row through a one-time upload isn't worth the
    # write load for a purely-informational raster cache that regenerates cheaply on its own.
    (
        46,
        "ALTER TABLE region_satellite ALTER COLUMN image DROP NOT NULL; "
        "ALTER TABLE region_satellite ALTER COLUMN labels DROP NOT NULL; "
        "ALTER TABLE region_satellite ADD COLUMN IF NOT EXISTS image_url TEXT; "
        "ALTER TABLE region_satellite ADD COLUMN IF NOT EXISTS labels_url TEXT",
    ),
    # issue #334 PR 3: activity-weighted backfill priority queue - the hourly/daily elevation
    # and precip backfill crons used to drain their backlog in pure ORDER BY id / observed_on
    # order (oldest first), so a long-dormant region's rows enriched before a currently-active
    # one's - backwards from what actually matters for what travelers see on a card today.
    # `refresh_backfill_queue` (re)populates this from `observations` (the source of truth,
    # re-derived each call rather than incrementally maintained - see that function's
    # docstring for why); `dequeue_backfill_batch` claims the highest-priority rows.
    (
        47,
        "CREATE TABLE IF NOT EXISTS backfill_queue ("
        "kind TEXT NOT NULL, obs_id BIGINT NOT NULL, priority DOUBLE PRECISION NOT NULL, "
        "enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (kind, obs_id)); "
        "CREATE INDEX IF NOT EXISTS ix_backfill_queue_priority ON backfill_queue (kind, priority DESC)",
    ),
    # issue #335 PR 2: persist the trail<->land-unit join instead of recomputing it live on every
    # `/api/trails` and trail-selection request (`queries.trail_land_units` / `get_trail`'s old
    # LATERAL point-in-polygon join, now retired). Columns only, deliberately no backfill UPDATE
    # here - a one-transaction UPDATE across every existing trail (~1.9M rows, same scale as
    # migration 45's trail_geometry split) is exactly the write-load mistake this project already
    # made once with the DEM elevation backfill: unattended (this runs inside cd.yml's
    # migrate-once deploy gate), all-or-nothing, and impossible to watch or safely interrupt.
    # `cache.backfill_trail_land` does the actual one-time backfill instead, as a separate
    # manually-run, batched, resumable pass; `_assign_trail_land` keeps new writes current from
    # here on - see `upsert_trails` (a new trail has no label yet) and `upsert_public_land` (a
    # land polygon changing may relabel existing trails).
    (
        48,
        "ALTER TABLE trails ADD COLUMN IF NOT EXISTS land_agency TEXT; "
        "ALTER TABLE trails ADD COLUMN IF NOT EXISTS land_unit TEXT",
    ),
    # issue #335 PR 2 follow-up: `_TRAIL_LAND_JOIN`'s `ORDER BY ST_Area(pl.geom::geometry)` -
    # "smallest polygon wins" - recomputed that area from scratch on every single trail<->land
    # lookup, for every candidate polygon overlapping the point, by decompressing the full
    # geometry (BLM/USFS polygons run to hundreds of KB - Tongass National Forest alone measured
    # ~21ms just to compute its area once). At table scale (`backfill_trail_land`,
    # `_assign_trail_land_paged`) that's the entire cost: `EXPLAIN ANALYZE` on a real 5000-row
    # batch showed 97s total, ~19ms/row, essentially all of it in this one computation - the
    # `id = ANY(...)` lookup itself took 110ms for all 5000. This inherited an old-code smell:
    # `trail_land_units`'s original docstring already flagged the same live `ST_Area` call as
    # "too slow for 500 rows" and scoped it to a ~20-row card list only - PR 2 then ran that exact
    # per-lookup cost across the whole table without also fixing the cost itself.
    # `area_deg2` computes the area once, at write time (`trg_public_land_geom_area`, chained
    # after the existing `trg_public_land_geom` by trigger-name sort order so `NEW.geom` is
    # already set), off ~12k `public_land` rows instead of millions of trail lookups - a plain
    # numeric column sort at read time instead of a per-candidate geometry decompress + area calc.
    (
        49,
        """
        ALTER TABLE public_land ADD COLUMN IF NOT EXISTS area_deg2 DOUBLE PRECISION;
        -- Backfill before the trigger below exists, not after: a plain UPDATE once the trigger
        -- is live would fire it too, recomputing ST_Area(geom::geometry) a second time for the
        -- same row - undermining the "computed once per write" point of this migration, and
        -- needlessly decompressing the biggest geometries twice during the deploy itself
        -- (a Copilot review catch, PR #354).
        UPDATE public_land SET area_deg2 = ST_Area(geom::geometry) WHERE geom IS NOT NULL;
        CREATE OR REPLACE FUNCTION foray_public_land_area() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.area_deg2 := CASE WHEN NEW.geom IS NULL THEN NULL ELSE ST_Area(NEW.geom::geometry) END;
            RETURN NEW;
        END;
        $$;
        CREATE OR REPLACE TRIGGER trg_public_land_geom_area BEFORE INSERT OR UPDATE ON public_land
            FOR EACH ROW EXECUTE FUNCTION foray_public_land_area();
        """,
    ),
    # issue #337: replace the plain lat/lng degree grid (`floor(coord / cell_deg)`, the retired
    # config field - not related to the new `h3_resolution`) with H3 hexagons - the square grid
    # narrowed with `cos(lat)` going north (a 0.25 deg cell was ~28km wide at the equator, ~14km
    # wide in Alaska), which both under/over-sizes destination cards
    # by latitude and is the root cause of the offshore-cell-center bug #326 C4 patched
    # tactically. H3 cells are the same real-world size everywhere. Resolution 4 chosen
    # empirically (issue #337 scoping comment): against 1.99M local research-grade
    # observations it produced *denser* phenology cells than the old grid (17.9% of cells with
    # <=3 observations, vs. 25.4% today), where resolution 5 made sparsity worse (30.5%).
    #
    # `h3` (not `h3_postgis`) is enough - every region-keyed table here stores plain
    # lat/lng/TEXT columns, never a PostGIS geometry, so the postgis-specific wrapper functions
    # (which additionally require `postgis_raster`) buy nothing. DO managed Postgres exposes
    # `h3` directly to `CREATE EXTENSION` on its Standard-Edition plan, PG 15-17 (confirmed
    # 2026-09-10) - same as `postgis` above, no ansible/cluster-config change needed.
    #
    # Old and new grids don't overlap, so this is a wipe, not a translation - every dropped
    # table below self-heals on its own existing refresh path (`build_phenology`'s
    # DROP-and-rename already handles a from-scratch build; `precip_daily`/`precipitation`
    # repopulate from the `precip-backfill`/`precip-refresh` cron jobs; `region_places`
    # repopulates lazily on next view via `layers.py`'s reverse-geocode path).
    # `region_satellite` is the one real cost: its cached Esri exports (25-45s/region) are gone
    # too, so a deliberate post-migration `foray backfill-satellite` run is worth doing rather
    # than waiting on first-view fetches for every region - see the issue for that rollout note.
    #
    # ROLLOUT (Copilot review, PR #370): dropping `phenology`/`regions` here means
    # `/api/destinations` serves nothing until the next `build_phenology` call - unlike the
    # other tables above, this isn't lazy/cron-driven on its own schedule, it rides on the next
    # ingest run (`refresh.run_home_refresh` calls it unconditionally on every "mushrooms"
    # target, not debounced - only the backfill-driven path in `maybe_rebuild_phenology` is).
    # Bounded to at most one nightly `ingest` cron cycle, same "deliberately not baked into the
    # migration itself" call as the #335 PR2 trail-land backfill - a schema migration is the
    # wrong place for a ~20s-per-million-rows compute pass with its own failure modes (a fresh
    # `regions`/`phenology` table needs an `ANALYZE` and index build too). Run
    # `foray refresh --with mushrooms` manually right after deploy to skip that wait instead of
    # leaving destinations empty until the next scheduled ingest.
    (
        50,
        """
        CREATE EXTENSION IF NOT EXISTS h3;
        DROP TABLE IF EXISTS phenology, regions, observations_scoring;
        TRUNCATE precip_daily, precipitation, region_satellite, region_places;
        """,
    ),
    # issue #380: pinning gfs_seamless/era5_seamless on the precip fetches (precip.py) changed
    # what every existing precip_daily/precipitation/observations.precip_*_mm row means, but
    # `backfill_precip`'s pending-set and `refresh_precipitation`'s TTL only revisit rows that
    # are NULL or stale - already-enriched data would silently keep the old default-blend values
    # forever (Copilot review, PR #381). Same fix migration 50 used for the H3 grid cutover: wipe
    # the two cache tables and null the derived observation columns so the existing self-healing
    # backfill/refresh jobs (already running on cron, no one-off ops step needed) repopulate
    # everything with the new models on their normal schedule.
    (
        51,
        """
        TRUNCATE precip_daily, precipitation;
        UPDATE observations SET precip_7d_mm = NULL, precip_30d_mm = NULL
        WHERE precip_7d_mm IS NOT NULL OR precip_30d_mm IS NOT NULL;
        """,
    ),
    # issue #394: trails.py used to cache a route=hiking relation's member ways twice - once as
    # the route's own stitched row, once again as each member's standalone `kind='path'` row -
    # so the map drew the same trail twice (each row independently vertex-thinned, so the two
    # lines never quite lined up). `_TRAILS_QUERY_VERSION` 5 stops new duplicates from being
    # cached, but a version bump only triggers a re-pull, it doesn't delete what's already
    # cached, so the ~94k existing duplicate `path` rows (measured nationwide at the time of the
    # fix) need a one-off cleanup.
    #
    # `ST_DWithin(p.geom, r.geom, 5)` (Copilot review, PR #395) is too loose for that: it's true
    # the moment *any* point of the path comes within 5m of *any* point of the route - which a
    # genuinely distinct trail can do at a crossing, a shared trailhead, or a short paralleling
    # stretch, without actually being one of the route's own member ways. Requiring the path's
    # *entire* line to sit inside a 5m buffer around the route (`ST_CoveredBy` on a buffered
    # route geography) only matches a path whose whole geometry is essentially the route's own
    # line re-cached - a path that merely touches or briefly runs beside a route, but extends
    # beyond that buffer, is correctly left alone.
    (
        52,
        """
        DELETE FROM trails p
        USING trails r
        WHERE p.kind = 'path'
          AND r.kind = 'route'
          -- cheap index-accelerated prefilter (uses ix_trails_geom_notnull) - ST_CoveredBy alone
          -- has no such index support, so this keeps the join from scanning every path x route
          -- pair with a per-pair ST_Buffer call
          AND ST_DWithin(p.geom, r.geom, 5)
          AND ST_CoveredBy(p.geom, ST_Buffer(r.geom, 5));
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


def _invalidate_rank_cache() -> None:
    """Drop the in-process ranking cache (issue #333 PR 2) after a trails/campsites/fire write.

    ``rank_destinations``/``rank_destinations_corridor`` fold ``_apply_access`` (trails +
    campsites) and ``_apply_fire`` (fire_perimeters) into a cached result that otherwise only
    busts on a phenology rebuild - without this, a card scored right after one of these upserts
    (an ingest run, a test asserting a before/after score change) would read a stale
    pre-upsert score until the TTL backstop lapses. Local import: avoid a cache<->scoring
    import cycle (same reason ``maybe_rebuild_phenology`` imports ``build_phenology`` locally).
    """
    from foray.scoring import rank_cache

    rank_cache.invalidate()


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


def copy_insert_ignore(
    con: psycopg.Connection,
    table: LiteralString,
    columns: Sequence[LiteralString],
    rows: Sequence[tuple[Any, ...]],
    *,
    conflict: LiteralString = "id",
) -> int:
    """Like :func:`copy_upsert`, but ``ON CONFLICT (conflict) DO NOTHING`` instead of updating -
    a row already present is left completely untouched. For a bulk historical loader
    (``foray.sources.inat_bulk.load_inat``, issue #334 PR 2) reading from a dated snapshot that
    can be days-to-weeks old: unlike the live ingest/resync/revalidate paths (which always carry
    iNat's *current* truth, so overwriting is correct), an older snapshot value must never win
    over a row a live path already wrote or corrected since - insert-only makes that regression
    structurally impossible instead of relying on job scheduling to avoid the overlap. Rows this
    skips are exactly the ones already covered by ordinary ingest; nothing is lost. Returns the
    number of rows attempted (not the number actually inserted - some may already exist).
    """
    if not rows:
        return 0
    key_idx = list(columns).index(conflict)
    deduped = list({row[key_idx]: row for row in rows}.values())
    collist = ", ".join(columns)
    create_stg: LiteralString = (
        f"CREATE TEMP TABLE _copy_stg ON COMMIT DROP AS SELECT {collist} FROM {table} WITH NO DATA"
    )
    copy_in: LiteralString = f"COPY _copy_stg ({collist}) FROM STDIN"
    insert_select: LiteralString = (
        f"INSERT INTO {table} ({collist}) SELECT {collist} FROM _copy_stg ON CONFLICT ({conflict}) DO NOTHING"
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
    whole ``SCHEMA`` string (and the CONCURRENTLY probe) on every CLI call and cron tick.

    issue #332: this is also the natural shape for a future serve-time schema-version guard -
    ``create_app``'s lifespan (``api/app.py``) could call this (or a cheap variant of it)
    *before* ``apply_schema`` and refuse to start serving on a mismatch, instead of every
    cron container + API instance racing ``apply_schema`` against a moving target during a
    rolling deploy (the #277 footgun). Not wired up here - `cd.yml` needs a dedicated
    migrate-once step first (see the CI/CD notes there) so there's always exactly one writer
    for a schema version before any reader can be asked to gate on it.
    """
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
        succeeded_guards: set[str] = set()
        for entry in _CONCURRENT_INDEXES:
            statement, guard = entry if isinstance(entry, tuple) else (entry, None)
            is_drop = statement.startswith("DROP")
            if is_drop and guard is not None and guard not in succeeded_guards:
                indexes_ok = False
                logger.warning(
                    "cache: skipping %r - its replacement index did not build successfully "
                    "this run, so dropping the old one would leave the table with neither.",
                    statement,
                )
                continue
            try:
                con.execute(statement)
                if guard is not None and not is_drop:
                    succeeded_guards.add(guard)
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
