"""Thin wrapper over DO Spaces' S3-compatible API (issue #334 PR 1).

Two consumers: `cache.save_region_satellite`/`load_region_satellite` (an object per region,
read back via a public URL) and `foray.ingest_bulk` (dated snapshot staging for the future
bulk-source loaders, `bulk/{source}/{date}/...`). Both go through the same client + bucket -
one Space, prefixed by use, rather than provisioning a second one.

Unconfigured (`Settings.spaces.configured is False`, the default) is a supported state: callers
fall back to Postgres-only behavior rather than erroring, matching the ansible-side
`foray_spaces_access_key_id` "unset skips it" convention for the basemap Space.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from datetime import date
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from foray.config import Spaces

logger = logging.getLogger(__name__)


class SpacesNotConfigured(RuntimeError):
    """Raised when a Spaces-only operation is attempted without credentials configured."""


def client(cfg: Spaces) -> Any:
    """A boto3 S3 client pointed at the DO Spaces endpoint. Raises `SpacesNotConfigured` if
    `cfg` has no credentials - callers that have an unconfigured fallback should check
    `cfg.configured` first rather than catching this."""
    if not cfg.configured:
        raise SpacesNotConfigured("FORAY_SPACES__ACCESS_KEY_ID/SECRET_ACCESS_KEY/BUCKET not set")
    # Imported lazily: boto3 is only needed by the Spaces-backed path, not every process that
    # imports this module (e.g. a dev box running entirely off the Postgres fallback).
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        region_name=cfg.region,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
    )


def put_object(cfg: Spaces, key: str, data: bytes, content_type: str, *, public: bool = False) -> str:
    """Upload `data` to `key`, returning its public URL (still returned when `public=False` -
    the bucket's default ACL applies, and the URL is only actually fetchable by a caller with
    credentials via `object_bytes`, not a browser). Private by default: bulk-snapshot uploads
    (`foray.ingest_bulk`) are read back server-side with credentials, not by an anonymous
    client, and would otherwise be world-downloadable the moment a stager runs. `region_satellite`
    passes `public=True` deliberately - its rasters are served straight through to the browser."""
    extra = {"ACL": "public-read"} if public else {}
    client(cfg).put_object(Bucket=cfg.bucket, Key=key, Body=data, ContentType=content_type, **extra)
    return f"{cfg.base_url}/{key}"


def object_bytes(cfg: Spaces, key: str) -> bytes:
    """Fetch a *small* object's bytes directly from the Space (not via its public URL) - the
    snapshot manifest, not the snapshot data itself. A staged Parquet/GPKG can be tens of GB on
    a 2 GB droplet; loading one through this would OOM before COPY even starts - use
    `download_file` for those instead."""
    body = client(cfg).get_object(Bucket=cfg.bucket, Key=key)["Body"]
    return body.read()


def download_file(cfg: Spaces, key: str, dest_path: str) -> None:
    """Stream an object straight to disk via boto3's managed download (bounded memory,
    multipart-aware) - the loader path for a staged snapshot file, where `object_bytes` would
    materialize the whole thing in memory first."""
    client(cfg).download_file(cfg.bucket, key, dest_path)


def upload_file(cfg: Spaces, key: str, src_path: str, content_type: str) -> None:
    """Stream a local file straight to `key` via boto3's managed upload (bounded memory,
    multipart-aware) - `download_file`'s counterpart, for a stager that streamed its Parquet
    snapshot to a local tempfile (`ingest_bulk.write_snapshot_parquet`) rather than building it
    in an in-memory buffer first."""
    client(cfg).upload_file(src_path, cfg.bucket, key, ExtraArgs={"ContentType": content_type})


# Per-run isolation (issue #334 PR 1 review): two `stage_snapshot` runs for the same
# source/date - a deliberate re-stage, or an overlapping/retried run - must never be able to
# produce a mixed read of one run's objects and another's. Each run uploads under its own
# unique `run_id` subprefix, so no two runs ever touch the same key; `publish_snapshot` then
# does the one operation that has to be atomic (a single PUT of a small manifest naming which
# run_id is current), after every object under that run's prefix has already landed. A reader
# always resolves the current run_id first and only reads under that run's prefix - it never
# lists the date prefix directly, so an abandoned/superseded run's leftover objects are just
# inert, never visible as part of a snapshot.
_MANIFEST_NAME = "_manifest.json"


def new_run_id() -> str:
    return uuid.uuid4().hex


def snapshot_prefix(source: str, snapshot_date: date) -> str:
    """The fixed per-date key prefix holding a snapshot's manifest (not its data - see
    `snapshot_run_prefix`)."""
    return f"bulk/{source}/{snapshot_date.isoformat()}/"


def snapshot_run_prefix(source: str, snapshot_date: date, run_id: str) -> str:
    """The key prefix one staging run uploads its data under. Unique per run, so a stager can
    freely re-upload a date (or two stagers can race for it) without either ever overwriting or
    partially-shadowing the other's objects."""
    return f"{snapshot_prefix(source, snapshot_date)}runs/{run_id}/"


def publish_snapshot(cfg: Spaces, source: str, snapshot_date: date, run_id: str) -> None:
    """Atomically make `run_id` the current snapshot for `source`/`snapshot_date`. Call this
    last, only after every object under `snapshot_run_prefix(source, snapshot_date, run_id)` has
    landed - the manifest PUT is the one moment a reader can observe a state change, and it's a
    single object write (atomic on S3-compatible storage), so a reader never sees a run before
    it's fully uploaded."""
    manifest_key = f"{snapshot_prefix(source, snapshot_date)}{_MANIFEST_NAME}"
    client(cfg).put_object(
        Bucket=cfg.bucket,
        Key=manifest_key,
        Body=json.dumps({"run_id": run_id}).encode(),
        ContentType="application/json",
    )


def snapshot_run_id(cfg: Spaces, source: str, snapshot_date: date) -> str | None:
    """The `run_id` currently published for `source`/`snapshot_date`, or ``None`` if no run has
    ever been published for that date."""
    from botocore.exceptions import ClientError

    manifest_key = f"{snapshot_prefix(source, snapshot_date)}{_MANIFEST_NAME}"
    try:
        body = object_bytes(cfg, manifest_key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(body)["run_id"]


def _candidate_snapshot_dates(cfg: Spaces, source: str) -> set[date]:
    """Every ``YYYY-MM-DD`` prefix under ``bulk/{source}/`` - one paginated listing, no manifest
    lookups yet (a candidate date may or may not actually have a published manifest)."""
    prefix = f"bulk/{source}/"
    paginator = client(cfg).get_paginator("list_objects_v2")
    dates: set[date] = set()
    for page in paginator.paginate(Bucket=cfg.bucket, Prefix=prefix, Delimiter="/"):
        for common_prefix in page.get("CommonPrefixes", []):
            raw = common_prefix["Prefix"].removeprefix(prefix).rstrip("/")
            try:
                dates.add(date.fromisoformat(raw))
            except ValueError:
                logger.warning("spaces: skipping non-date prefix %s under %s", raw, prefix)
    return dates


def list_snapshot_dates(cfg: Spaces, source: str) -> list[date]:
    """Every ``YYYY-MM-DD`` with a published manifest under ``bulk/{source}/``, ascending. Empty
    if none staged yet, or if the prefix doesn't exist - not an error, since a source's first
    snapshot has to start somewhere. One manifest lookup per candidate date - callers that only
    need the newest (e.g. a frequently-polled freshness check) should use
    `latest_snapshot_date` instead, which stops at the first hit instead of checking every date
    a source has ever staged."""
    return sorted(
        snapshot_date
        for snapshot_date in _candidate_snapshot_dates(cfg, source)
        if snapshot_run_id(cfg, source, snapshot_date) is not None
    )


# Bounds how often `latest_snapshot_date` re-lists a source's date prefixes (Copilot review, PR
# #358): the newest-first manifest-GET short-circuit alone still repeats the paginated
# ListObjectsV2 listing itself on every call, which `/healthz/data` may make far more often than
# this 5-minute window - a health check doesn't need second-to-second freshness against a
# pipeline that stages weekly.
_LATEST_SNAPSHOT_CACHE_TTL_SECONDS = 300.0
_latest_snapshot_cache_lock = threading.Lock()
_latest_snapshot_cache: dict[tuple[str, str], tuple[float, date | None]] = {}


def invalidate_latest_snapshot_cache() -> None:
    """Test-only reset - a real process never needs this, the TTL alone bounds staleness."""
    with _latest_snapshot_cache_lock:
        _latest_snapshot_cache.clear()


def latest_snapshot_date(cfg: Spaces, source: str) -> date | None:
    """The newest published snapshot date for `source`, or ``None`` if none has ever published.
    Checks candidate dates newest-first and stops at the first with a published manifest - O(1)
    manifest lookups in the common case (the newest date is already published), unlike
    `list_snapshot_dates`'s full-history scan, whose per-date manifest GETs would otherwise grow
    unbounded as a weekly-staged source accumulates dates. Result is cached in-process for
    `_LATEST_SNAPSHOT_CACHE_TTL_SECONDS` (keyed on bucket + source, since one process can only
    ever run against one Spaces config at a time - see `foray.scoring.rank_cache` for the same
    single-uvicorn-process assumption) - the shape `/healthz/data` (issue #357) needs, since it
    may be polled far more often than a source stages."""
    cache_key = (cfg.bucket, source)
    now = time.monotonic()
    with _latest_snapshot_cache_lock:
        cached = _latest_snapshot_cache.get(cache_key)
        if cached is not None and now - cached[0] < _LATEST_SNAPSHOT_CACHE_TTL_SECONDS:
            return cached[1]
    result = None
    for candidate in sorted(_candidate_snapshot_dates(cfg, source), reverse=True):
        if snapshot_run_id(cfg, source, candidate) is not None:
            result = candidate
            break
    with _latest_snapshot_cache_lock:
        _latest_snapshot_cache[cache_key] = (now, result)
    return result


def prune_other_snapshots(cfg: Spaces, source: str, keep_date: date, keep_run_id: str) -> int:
    """Delete every object under `bulk/{source}/` except the one just-published run (issue #359
    PR 3) - both stale prior-week dates and any orphaned same-day re-stage run. Only ever called
    *after* `publish_snapshot` has succeeded (never before - pruning first risks a concurrent
    loader losing the snapshot it's mid-download of, since `list_snapshot_dates`/
    `latest_snapshot_date` resolve the manifest first but a loader's actual `download_file` calls
    happen afterward, against keys this would have already deleted). Retention here is one
    snapshot per source, not history - minimizes the monthly Spaces bill rather than preserving
    old runs (a deliberate choice, not an oversight - see this module's per-run-isolation
    docstring for why an orphaned run's objects were left in place before this existed).

    Not safe against two overlapping `stage_snapshot` calls for the same `source` (Copilot
    review, PR #361): if run A publishes and prunes while run B has uploaded objects but not yet
    published, A's prune deletes B's not-yet-published objects (they don't match A's
    `keep_prefix` and aren't the manifest key), so B's later publish points at data that's gone.
    `ingest_bulk.stage_snapshot`'s docstring covers the actual mitigation - a GitHub Actions
    `concurrency` group on `bulk-load.yml`, the one place this ever runs from - rather than
    S3-level locking here, since DO Spaces' conditional-write support isn't guaranteed.

    Returns the number of objects actually deleted. Raises `RuntimeError` if `delete_objects`
    reports any per-key failures - S3 returns those as a 200 response with an `Errors` list, not
    as a raised exception, so a caller must not read a returned count as "n objects pruned" when
    the batch it came from silently left some objects behind.

    One recursive listing (no ``Delimiter``, unlike `_candidate_snapshot_dates`) plus batched
    `delete_objects` calls (S3's 1000-key cap per call)."""
    keep_prefix = snapshot_run_prefix(source, keep_date, keep_run_id)
    manifest_key = f"{snapshot_prefix(source, keep_date)}{_MANIFEST_NAME}"
    prefix = f"bulk/{source}/"
    s3 = client(cfg)
    paginator = s3.get_paginator("list_objects_v2")
    doomed: list[str] = []
    for page in paginator.paginate(Bucket=cfg.bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key == manifest_key or key.startswith(keep_prefix):
                continue
            doomed.append(key)
    deleted = 0
    for start in range(0, len(doomed), 1000):
        batch = doomed[start : start + 1000]
        response = s3.delete_objects(Bucket=cfg.bucket, Delete={"Objects": [{"Key": key} for key in batch]})
        errors = response.get("Errors") or []
        if errors:
            raise RuntimeError(f"spaces: prune_other_snapshots failed to delete {len(errors)} object(s): {errors!r}")
        deleted += len(batch)
    if deleted:
        logger.info("spaces: pruned %d stale object(s) under %s, keeping %s", deleted, prefix, keep_prefix)
    return deleted


# The standard bulk-snapshot format (issue #359 PR 1/2), replacing the jsonl.gz pattern
# `inat_bulk`/`camps`/`usfs_trails` independently converged on. 5000 matches `inat_bulk`'s old
# DB-chunk-insert batch size - large enough for Parquet's columnar/dictionary encoding to pay
# off, small enough that a stager/loader never buffers more than one batch's worth of rows.
# Lives here, not `ingest_bulk.py`, because `ingest_bulk` imports every stager/loader module for
# its STAGERS/LOADERS registry - a stager importing these back from `ingest_bulk` would be a
# circular import.
_PARQUET_ROW_GROUP_SIZE = 5000


def write_snapshot_parquet(
    cfg: Spaces,
    source: str,
    snapshot_date: date,
    run_id: str,
    filename: str,
    rows: Iterable[dict[str, Any]],
    schema: pa.Schema,
) -> int:
    """Stream `rows` (each a dict keyed by `schema`'s field names) into a Parquet file and
    upload it under this run's Space prefix - the shared write path every stager uses instead of
    each hand-rolling its own tempfile/gzip/put_object dance. Batches rows into
    `_PARQUET_ROW_GROUP_SIZE`-row `pyarrow.RecordBatch`es and streams them to a local tempfile via
    `pyarrow.parquet.ParquetWriter`, then uploads that file via `upload_file` (bounded memory,
    multipart-aware) rather than building the whole encoded snapshot in memory first - matters
    most for `inat`, whose Fungi/US subset can run into the hundreds of thousands of rows even
    after streaming the source 29 GB archive down to just that.

    Returns the number of rows written."""
    count = 0
    batch: list[dict[str, Any]] = []
    with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
        writer = pq.ParquetWriter(tmp.name, schema)
        try:
            for row in rows:
                batch.append(row)
                count += 1
                if len(batch) >= _PARQUET_ROW_GROUP_SIZE:
                    writer.write_batch(pa.RecordBatch.from_pylist(batch, schema=schema))
                    batch = []
            if batch:
                writer.write_batch(pa.RecordBatch.from_pylist(batch, schema=schema))
        finally:
            writer.close()
        key = snapshot_run_prefix(source, snapshot_date, run_id) + filename
        upload_file(cfg, key, tmp.name, "application/vnd.apache.parquet")
    return count


def read_snapshot_parquet(
    cfg: Spaces,
    source: str,
    snapshot_date: date,
    run_id: str,
    filename: str,
    *,
    batch_size: int = _PARQUET_ROW_GROUP_SIZE,
) -> Iterator[list[dict[str, Any]]]:
    """Download one staged Parquet snapshot file and yield it back in `batch_size`-row batches of
    plain dicts - `write_snapshot_parquet`'s read-side counterpart. A loader's existing chunked
    upsert/insert loop needs no change beyond swapping its jsonl-parsing loop for this;
    `download_file` streams the object straight to a local tempfile first (bounded memory,
    multipart-aware), same as the old jsonl.gz loaders did."""
    key = snapshot_run_prefix(source, snapshot_date, run_id) + filename
    with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
        download_file(cfg, key, tmp.name)
        parquet_file = pq.ParquetFile(tmp.name)
        for record_batch in parquet_file.iter_batches(batch_size=batch_size):
            yield record_batch.to_pylist()
