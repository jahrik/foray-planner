"""``/healthz`` (liveness) + ``/healthz/data`` (freshness) - issue #332. ``/healthz/backlog``
(issue #334 PR 3) is the backlog/drain-rate gauge that issue's E4 asked for.

Neither of the first two existed before #332; cron jobs and the API both ran with no automated
signal beyond "did the process crash". ``/healthz`` answers "is this process able to serve a
request at all" (no DB round trip - a slow/down Postgres shouldn't make the load balancer kill
an otherwise-healthy API container). ``/healthz/data`` answers "is the data behind it fresh",
reusing ``/api/coverage``'s per-region latest-ingest-timestamp logic (``cache.latest_ingest_at``,
generalized from that route's inline query) for the layers that mark their ingests in
``ingest_log``, and the newest ``job_runs`` success for the two that don't (fire's replace-
semantics refresh, the recent-rain-per-destination layer - see ``foray.jobs``).

``/healthz/backlog`` answers a different question - not "is this stale" but "how much is
outstanding and how fast is it draining" - for the two backfills issue #334 PR 3 put behind an
activity-weighted priority queue (``cache.backfill_queue``). Informational only: nothing here
ever 503s, since a nonzero backlog is normal operation, not a failure.

``/healthz/data`` also covers *staging* freshness for every ``ingest_bulk.STAGERS``-registered
source (issue #357), not just loading - a source can be registered in code and still silently
never actually staged (the exact gap that left `inat`/`ridb` unstaged for weeks: nothing was
watching the DO Space's own published-snapshot dates). One `bulk-stage:{source}` layer per
registered source, `stale` if the newest published snapshot (``spaces.latest_snapshot_date`` -
newest-first, stops at the first published manifest rather than checking a source's whole
history) is more than 14 days old (2x `bulk-load.yml`'s weekly cadence) or none has ever
published. Skipped
entirely when Spaces isn't configured (local dev) - matching the `camps`/`RIDB_API_KEY` pattern
above, an unconfigured optional dependency isn't a freshness problem to report.

``/metrics`` (issue #338 PR 1) is a Prometheus text-exposition endpoint over the same
`job_runs`/`backfill_queue` data plus the DB-backed half of `/healthz/data`'s layer freshness
(`compute_layer_freshness`, factored out below so both routes share one query) - job run counts/
durations/rows/429s by job, backfill depth + drain rate by kind, and layer age/staleness gauges.
Deliberately excludes the Spaces-backed `bulk-stage:*` layers: those need an S3 `list_objects`
call per source, and a Prometheus scrape (every 15-60s, possibly from multiple scrapers) hitting
Spaces that often isn't worth it for a signal `/healthz/data` already exposes on its own slower,
cron-driven cadence.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import psycopg
from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from psycopg_pool import ConnectionPool

from foray.api.deps import get_pool, get_state
from foray.api.state import AppState
from foray.api_models import BacklogResponse, DataHealthResponse, LayerFreshnessResponse, StatusResponse
from foray.cache import backfill_queue_depth, job_run_drain_rate, latest_ingest_at, latest_successful_job_run
from foray.config import Settings
from foray.ingest_bulk import STAGERS
from foray.metrics import ForayCollector
from foray.spaces import latest_snapshot_date

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/healthz")
def healthz() -> StatusResponse:
    """Liveness only - the process can answer a request. No DB dependency on purpose: a
    Postgres blip should surface via `/healthz/data` going stale, not take the whole
    container out of rotation."""
    return StatusResponse(status="ok")


# (layer name, ingest_log key prefix or None, job name for job_runs-backed layers, interval,
# blocking). `intervals` on Settings carries the hour numbers; layers sharing a cadence
# (land/trails/camps/dispersed all ride the weekly `refresh --with ... --all`) share
# `layers_hours`. `blocking=False` layers still report their staleness but don't force a 503 -
# fire/precip aren't scheduled in prod cron yet (#332 PR 2 adds them; flip to blocking once
# they are). `camps` is dropped entirely, not just non-blocking, when RIDB_API_KEY is unset -
# it's a documented-optional source (see sources/camps.py), so "never configured" isn't a
# freshness problem to report at all.
def _layer_specs(cfg: Settings) -> list[tuple[str, str | None, str | None, float, bool]]:
    intervals = cfg.intervals
    specs = [
        ("observations", "obs:fungi:", None, intervals.ingest_hours, True),
        ("land", "land:", None, intervals.layers_hours, True),
        ("trails", "trails:", None, intervals.layers_hours, True),
        ("dispersed", "dispersed:", None, intervals.layers_hours, True),
        ("fire", None, "fire", intervals.fire_hours, False),
        ("precip", None, "refresh-precip", intervals.precip_hours, False),
    ]
    if os.getenv("RIDB_API_KEY"):
        specs.insert(3, ("camps", "camps:", None, intervals.layers_hours, True))
    return specs


def compute_layer_freshness(cfg: Settings, conn: psycopg.Connection, now: dt.datetime) -> list[LayerFreshnessResponse]:
    """The DB-only half of `/healthz/data` (everything but the Spaces-backed `bulk-stage:*`
    layers) - factored out so `/metrics` (issue #338 PR 1) can reuse the exact same freshness
    computation instead of re-deriving it."""
    multiplier = cfg.observability.data_freshness_multiplier
    layers: list[LayerFreshnessResponse] = []
    for name, prefix, job, interval_hours, blocking in _layer_specs(cfg):
        last_success = latest_ingest_at(conn, prefix) if prefix else None
        if last_success is None and job:
            run = latest_successful_job_run(conn, job)
            if run is not None:
                last_success = run["ended_at"] or run["started_at"]
        # The pool opens connections in autocommit with no explicit session timezone, so a
        # TIMESTAMPTZ column can come back naive (server-local) rather than UTC-aware -
        # normalize before comparing against `now` (always UTC-aware) or the subtraction
        # raises instead of just being wrong.
        if last_success is not None and last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=dt.UTC)
        stale = last_success is None or (now - last_success) > dt.timedelta(hours=interval_hours * multiplier)
        layers.append(
            LayerFreshnessResponse(
                layer=name,
                last_success=last_success.isoformat() if last_success else None,
                interval_hours=interval_hours,
                stale=stale,
                blocking=blocking,
            )
        )
    return layers


@router.get(
    "/healthz/data",
    responses={
        503: {
            "model": DataHealthResponse,
            "description": "At least one layer's latest success is older than its expected interval.",
        }
    },
)
def healthz_data(
    response: Response,
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> DataHealthResponse:
    """Freshness: any layer whose latest success is older than
    ``interval_hours * observability.data_freshness_multiplier`` (default 2x) fails the check
    - a non-200 response a cron/alerting layer or an uptime monitor can page on."""
    cfg = state.cfg
    now = dt.datetime.now(dt.UTC)
    with pool.connection() as conn:
        layers = compute_layer_freshness(cfg, conn, now)
    if cfg.spaces.configured:
        layers.extend(_bulk_stage_freshness(cfg, now))
    ok = not any(layer.stale and layer.blocking for layer in layers)
    if not ok:
        response.status_code = 503
    return DataHealthResponse(ok=ok, layers=layers)


# 2x bulk-load.yml's weekly cron - a fixed threshold, not `observability.data_freshness_multiplier`
# (that knob is about how much slack ingest/refresh cadences get, unrelated to this pipeline).
_BULK_STAGE_STALE_AFTER = dt.timedelta(days=14)


def _bulk_stage_freshness(cfg: Settings, now: dt.datetime) -> list[LayerFreshnessResponse]:
    """One `bulk-stage:{source}` layer per `ingest_bulk.STAGERS`-registered source - stale if
    the newest published snapshot is more than 14 days old, or none has ever published. A
    Spaces listing failure (network blip, throttling) degrades to reporting stale rather than
    raising - a real 503 either way, but with a source name attached instead of a generic
    500."""
    layers = []
    for source in sorted(STAGERS):
        try:
            newest = latest_snapshot_date(cfg.spaces, source)
        except Exception:
            logger.warning("healthz/data: failed listing snapshots for bulk source %s", source, exc_info=True)
            newest = None
        last_success = dt.datetime.combine(newest, dt.time.min, tzinfo=dt.UTC) if newest else None
        stale = last_success is None or (now - last_success) > _BULK_STAGE_STALE_AFTER
        layers.append(
            LayerFreshnessResponse(
                layer=f"bulk-stage:{source}",
                last_success=last_success.isoformat() if last_success else None,
                interval_hours=24 * 7,
                stale=stale,
                blocking=True,
            )
        )
    return layers


# (kind, the job whose job_runs feeds the drain rate). elevation-backfill-dem, not
# elevation-backfill, since the DEM job is the one actually expected to drain the bulk of the
# elevation backlog day to day (issue #334 PR 3) - the Open-Meteo job's own rate is a near-zero
# trickle once the DEM job has run.
_BACKLOG_SPECS = [
    ("elevation", "elevation-backfill-dem"),
    ("precip", "precip-backfill"),
]


@router.get("/healthz/backlog")
def healthz_backlog(pool: ConnectionPool = Depends(get_pool)) -> list[BacklogResponse]:
    """Backfill-queue depth + recent drain rate per kind (issue #334 PR 3) - never a failing
    check (no 503 here), just the numbers behind "is this catching up"."""
    with pool.connection() as conn:
        return [
            BacklogResponse(
                kind=kind,
                backlog=backfill_queue_depth(conn, kind),
                drain_rate_per_hour=job_run_drain_rate(conn, job),
                job=job,
            )
            for kind, job in _BACKLOG_SPECS
        ]


@router.get("/metrics", response_class=Response, responses={200: {"content": {CONTENT_TYPE_LATEST: {}}}})
def metrics(
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> Response:
    """Prometheus text-exposition scrape target (issue #338 PR 1) - see this module's
    docstring for scope. A fresh `CollectorRegistry` per request rather than one shared
    process-wide registry: `ForayCollector.collect()` already does all its work against
    Postgres on every call, so there's no in-process state a shared registry would actually
    be caching, and a fresh one sidesteps `prometheus_client`'s duplicate-registration error
    on module reload (e.g. multiple `TestClient` apps in the same test process). Not gated by
    any auth - matches `/healthz`/`/healthz/data`'s existing unauthenticated precedent;
    restricting scrape access (private network, firewall rule) is an ops decision for #338 PR
    2's Prometheus deployment, not this endpoint's job."""
    registry = CollectorRegistry()
    registry.register(ForayCollector(state.cfg, pool))
    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
