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

import logging
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
    """Fetch an object's bytes directly from the Space (not via its public URL) - used for
    server-side reads (e.g. staging-table loads) that shouldn't depend on the CDN/public path."""
    body = client(cfg).get_object(Bucket=cfg.bucket, Key=key)["Body"]
    return body.read()


# Marker object a stager writes last, after every other object under its snapshot prefix has
# landed - `list_snapshot_dates` only counts a date as available once this exists, so a loader
# can never pick up a partially-uploaded snapshot (a stager that dies mid-upload) or one an
# overlapping stage run is still writing to.
_SUCCESS_MARKER = "_SUCCESS"


def snapshot_prefix(source: str, snapshot_date: date) -> str:
    """The staging key prefix for one source's snapshot on a given date - both the GitHub
    Actions weekly stager (`foray stage-snapshot`) and the droplet-side loader (`foray
    ingest-bulk`) key off this same layout."""
    return f"bulk/{source}/{snapshot_date.isoformat()}/"


def mark_snapshot_complete(cfg: Spaces, source: str, snapshot_date: date) -> None:
    """Write the completion marker for a snapshot - call this last, only after every object the
    stager is uploading for `snapshot_date` has landed."""
    client(cfg).put_object(
        Bucket=cfg.bucket, Key=f"{snapshot_prefix(source, snapshot_date)}{_SUCCESS_MARKER}", Body=b""
    )


def list_snapshot_dates(cfg: Spaces, source: str) -> list[date]:
    """Every ``YYYY-MM-DD`` staged (and marked complete via `mark_snapshot_complete`) under
    ``bulk/{source}/``, ascending. Empty if none staged yet, or if the prefix doesn't exist - not
    an error, since a source's first snapshot has to start somewhere."""
    prefix = f"bulk/{source}/"
    paginator = client(cfg).get_paginator("list_objects_v2")
    dates: set[date] = set()
    for page in paginator.paginate(Bucket=cfg.bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(f"/{_SUCCESS_MARKER}"):
                continue
            raw = key.removeprefix(prefix).removesuffix(f"/{_SUCCESS_MARKER}")
            try:
                dates.add(date.fromisoformat(raw))
            except ValueError:
                logger.warning("spaces: skipping non-date snapshot marker %s under %s", raw, prefix)
    return sorted(dates)
