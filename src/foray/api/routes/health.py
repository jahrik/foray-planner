"""``/healthz`` (liveness) + ``/healthz/data`` (freshness) - issue #332.

Neither existed before this; cron jobs and the API both ran with no automated signal beyond
"did the process crash". ``/healthz`` answers "is this process able to serve a request at
all" (no DB round trip - a slow/down Postgres shouldn't make the load balancer kill an
otherwise-healthy API container). ``/healthz/data`` answers "is the data behind it fresh",
reusing ``/api/coverage``'s per-region latest-ingest-timestamp logic (``cache.latest_ingest_at``,
generalized from that route's inline query) for the layers that mark their ingests in
``ingest_log``, and the newest ``job_runs`` success for the two that don't (fire's replace-
semantics refresh, the recent-rain-per-destination layer - see ``foray.jobs``).
"""

from __future__ import annotations

import datetime as dt
import os

from fastapi import APIRouter, Depends, Response
from psycopg_pool import ConnectionPool

from foray.api.deps import get_pool, get_state
from foray.api.state import AppState
from foray.api_models import DataHealthResponse, LayerFreshnessResponse, StatusResponse
from foray.cache import latest_ingest_at, latest_successful_job_run
from foray.config import Settings

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
    multiplier = cfg.observability.data_freshness_multiplier
    now = dt.datetime.now(dt.UTC)
    layers: list[LayerFreshnessResponse] = []
    with pool.connection() as conn:
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
    ok = not any(layer.stale and layer.blocking for layer in layers)
    if not ok:
        response.status_code = 503
    return DataHealthResponse(ok=ok, layers=layers)
