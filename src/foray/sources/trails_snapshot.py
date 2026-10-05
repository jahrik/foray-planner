"""The bulk-snapshot format and loader shared by every ``trails`` source (issue #442).

USFS Trail_NFS (``usfs_trails``), USFS MVUM roads (``usfs_mvum``) and the OSM trail network
(``osm_trails``) all stage the same 11-column trails row to a Parquet file and load it the same
way, so the schema, the WKB round-trip and the loader live here once instead of in each module.

The loader is a *diff* load: ``cache.upsert_trails_changed`` writes only rows that are new or
differ from what's cached, ``cache.prune_trails_missing_from`` deletes only rows the source no
longer lists, and ``cache.prune_trail_duplicates_tiled`` dedups the tiles that changed. A weekly
national snapshot is overwhelmingly unchanged, so a routine load is a small write rather than a
rewrite of every row (the full-rewrite MVUM load took ~2.4 h on the 1-vCPU database).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from datetime import date
from typing import Any, LiteralString

import psycopg
import pyarrow as pa
import shapely

from foray import cache, spaces
from foray.cache import record_ingest
from foray.config import Settings

logger = logging.getLogger(__name__)

SNAPSHOT_FILENAME = "trails.parquet"
_CHUNK_SIZE = 5000
# Session temp tables holding one load's id sets (``cache.create_id_table``).
_LISTED: LiteralString = "_snapshot_listed"
_WRITTEN: LiteralString = "_snapshot_written"
_HELD_BACK: LiteralString = "_snapshot_held_back"

# Same order as the trails row tuple (``cache.upsert_trails``), except ``geometry_wkb`` (WKB
# bytes) replaces the tuple's ``geojson`` text - issue #359's geometry-encoding decision,
# compact and PostGIS-native. The ``trail_geometry`` insert trigger still wants GeoJSON text,
# so loading converts back (``record_to_row``).
TRAIL_COLUMNS = (
    "id",
    "name",
    "kind",
    "source",
    "url",
    "center_lat",
    "center_lng",
    "geometry_wkb",
    "connects",
    "length_km",
    "attrs",
)
SNAPSHOT_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("name", pa.string()),
        ("kind", pa.string()),
        ("source", pa.string()),
        ("url", pa.string()),
        ("center_lat", pa.float64()),
        ("center_lng", pa.float64()),
        ("geometry_wkb", pa.binary()),
        ("connects", pa.list_(pa.string())),
        ("length_km", pa.float64()),
        ("attrs", pa.string()),
    ]
)


def geojson_to_wkb(geojson_text: str) -> bytes:
    return shapely.to_wkb(shapely.from_geojson(geojson_text))


def wkb_to_geojson(wkb_bytes: bytes) -> str:
    return shapely.to_geojson(shapely.from_wkb(bytes(wkb_bytes)), indent=None)


def row_to_record(row: tuple[Any, ...]) -> dict[str, Any]:
    """A trails row tuple -> one Parquet record (GeoJSON text -> WKB)."""
    return dict(zip(TRAIL_COLUMNS, (*row[:7], geojson_to_wkb(row[7]), *row[8:]), strict=True))


def record_to_row(record: dict[str, Any]) -> tuple[Any, ...]:
    """One Parquet record -> a trails row tuple (WKB -> GeoJSON text)."""
    connects = record["connects"]
    return (
        record["id"],
        record["name"],
        record["kind"],
        record["source"],
        record["url"],
        record["center_lat"],
        record["center_lng"],
        wkb_to_geojson(record["geometry_wkb"]),
        list(connects) if connects else None,
        record["length_km"],
        record["attrs"],
    )


def write_snapshot(
    cfg: Settings, bulk_source: str, snapshot_date: date, run_id: str, rows: Iterable[tuple[Any, ...]]
) -> int:
    """Stream trails row tuples into this run's Parquet snapshot. Returns rows written."""
    return spaces.write_snapshot_parquet(
        cfg.spaces,
        bulk_source,
        snapshot_date,
        run_id,
        SNAPSHOT_FILENAME,
        (row_to_record(row) for row in rows),
        SNAPSHOT_SCHEMA,
    )


def _read_rows(cfg: Settings, bulk_source: str, snapshot_date: date, run_id: str) -> Iterator[list[tuple[Any, ...]]]:
    for batch in spaces.read_snapshot_parquet(
        cfg.spaces, bulk_source, snapshot_date, run_id, SNAPSHOT_FILENAME, batch_size=_CHUNK_SIZE
    ):
        yield [record_to_row(record) for record in batch]


def load_snapshot(
    con: psycopg.Connection,
    cfg: Settings,
    *,
    bulk_source: str,
    trails_source: str,
    snapshot_date: date,
    run_id: str,
    min_keep_ratio: float = 0.5,
) -> None:
    """Diff-load one staged snapshot into ``trails`` (rows with ``source = trails_source``).

    Deletes rows the snapshot no longer lists only when the snapshot holds at least
    ``min_keep_ratio`` of the rows already cached for that source: a snapshot that suddenly
    lost half a national dataset is far more likely truncated than real, and pruning on it
    would wipe good data. The new rows are still written either way."""
    # Fast land-ownership tagging for every row this load writes (built once, then kept current).
    cache.ensure_land_parts(con)
    # Set while rows are written but not yet deduped. A load that died in between (killed, OOM,
    # a failed query) left rows that a rerun's diff sees as unchanged - so it finds the marker
    # and dedups every tile of the source instead of only this run's written rows. The same
    # holds until a load of this source has ever completed: a first load that died before the
    # marker existed left no marker to find (Copilot review, PR #444).
    pending_key = f"trails_dedup_pending:{trails_source}"
    resume_full_dedup = (
        con.execute("SELECT 1 FROM meta WHERE key = %s", [pending_key]).fetchone() is not None
        or con.execute(
            "SELECT 1 FROM ingest_log WHERE key LIKE %s LIMIT 1", [f"trails:{trails_source}:bulk:%"]
        ).fetchone()
        is None
    )
    # A changed dedup rule has to re-judge pairs the old rule kept - rows a diff load never
    # rewrites, so only a full pass reaches them. Recorded per source once a full pass finishes.
    rule_key = f"trails_dedup_rule:{trails_source}"
    rule_row = con.execute("SELECT value FROM meta WHERE key = %s", [rule_key]).fetchone()
    rule_changed = rule_row is None or rule_row[0] != str(cache.TRAIL_DEDUP_RULE_VERSION)
    con.execute(
        "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
        [pending_key, snapshot_date.isoformat()],
    )
    con.commit()
    existing = con.execute("SELECT count(*) FROM trails WHERE source = %s", [trails_source]).fetchone()
    existing_count = existing[0] if existing else 0
    # The listed / written / tombstone-held-back id sets live in session temp tables, not Python:
    # a national snapshot is millions of ids, and holding them (plus passing them back as one
    # array) is what ran the 2 GB droplet out of memory on the first national OSM load.
    cache.create_id_table(con, _LISTED)
    cache.create_id_table(con, _WRITTEN)
    cache.create_id_table(con, _HELD_BACK)
    for chunk in _read_rows(cfg, bulk_source, snapshot_date, run_id):
        chunk_ids = [row[0] for row in chunk]
        cache.append_ids(con, _LISTED, chunk_ids)
        cache.append_ids(con, _WRITTEN, cache.upsert_trails_changed(con, chunk))
        cache.append_ids(con, _HELD_BACK, cache.tombstoned_ids(con, chunk_ids))
        con.commit()
    for table in (_LISTED, _WRITTEN, _HELD_BACK):
        con.execute("ANALYZE " + table)
    listed_count = _count(con, _LISTED)
    pruned = 0
    if existing_count and listed_count < min_keep_ratio * existing_count:
        logger.error(
            "%s: snapshot lists %d rows but %d are cached - not pruning (truncated snapshot?)",
            bulk_source,
            listed_count,
            existing_count,
        )
    else:
        pruned = cache.prune_trails_not_listed(con, trails_source, _LISTED)
    # A row held back by a tombstone loses it when its kept row is pruned (just above, cascading)
    # or rewritten (a later chunk) - Copilot review, PR #443. Write those now rather than leave
    # neither row cached until the next snapshot.
    released = {
        row[0]
        for row in con.execute(
            "SELECT h.id FROM " + _HELD_BACK + " h "
            "WHERE NOT EXISTS (SELECT 1 FROM trail_duplicates d WHERE d.osm_id = h.id)"
        ).fetchall()
    }
    if released:
        for chunk in _read_rows(cfg, bulk_source, snapshot_date, run_id):
            retry = [row for row in chunk if row[0] in released]
            if retry:
                cache.append_ids(con, _WRITTEN, cache.upsert_trails_changed(con, retry))
                con.commit()
    written_count = _count(con, _WRITTEN)
    # A new or changed row can duplicate one cached long before - dedup the tiles those rows
    # cross (bounded per tile, never one table-wide sweep).
    if resume_full_dedup or rule_changed:
        reason = "the previous load stopped before its dedup" if resume_full_dedup else "the dedup rule changed"
        logger.warning("%s: %s - deduping every tile", bulk_source, reason)
        cache.prune_trail_duplicates_tiled(con, trails_source)
        con.execute(
            "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [rule_key, str(cache.TRAIL_DEDUP_RULE_VERSION)],
        )
    elif written_count:
        cache.prune_trail_duplicates_tiled(con, trails_source, id_table=_WRITTEN)
    con.execute("DELETE FROM meta WHERE key = %s", [pending_key])
    con.commit()
    # Namespaced under "trails:" so /healthz/data's trails freshness picks this load up.
    record_ingest(con, f"trails:{trails_source}:bulk:{snapshot_date.isoformat()}", written_count)
    logger.info(
        "%s: snapshot of %d rows - wrote %d new/changed, pruned %d no longer listed",
        bulk_source,
        listed_count,
        written_count,
        pruned,
    )


def _count(con: psycopg.Connection, table: LiteralString) -> int:
    row = con.execute("SELECT count(*) FROM " + table).fetchone()
    return row[0] if row else 0
