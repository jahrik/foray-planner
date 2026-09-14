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
import uuid
from datetime import date
from typing import Any

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
    return sorted(d for d in _candidate_snapshot_dates(cfg, source) if snapshot_run_id(cfg, source, d) is not None)


def latest_snapshot_date(cfg: Spaces, source: str) -> date | None:
    """The newest published snapshot date for `source`, or ``None`` if none has ever published.
    Checks candidate dates newest-first and stops at the first with a published manifest - O(1)
    manifest lookups in the common case (the newest date is already published), unlike
    `list_snapshot_dates`'s full-history scan, whose per-date manifest GETs would otherwise grow
    unbounded as a weekly-staged source accumulates dates - the shape `/healthz/data` (issue
    #357) needs, since it may be polled far more often than a source stages."""
    for candidate in sorted(_candidate_snapshot_dates(cfg, source), reverse=True):
        if snapshot_run_id(cfg, source, candidate) is not None:
            return candidate
    return None
