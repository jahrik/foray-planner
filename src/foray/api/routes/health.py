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

from fastapi import APIRouter, Depends, Response
from psycopg_pool import ConnectionPool

from foray.api.deps import get_pool, get_state
from foray.api.state import AppState
from foray.api_models import DataHealthResponse, LayerFreshnessResponse, StatusResponse
from foray.cache import latest_ingest_at, latest_job_run
from foray.config import Settings

router = APIRouter()


@router.get("/healthz")
def healthz() -> StatusResponse:
    """Liveness only - the process can answer a request. No DB dependency on purpose: a
    Postgres blip should surface via `/healthz/data` going stale, not take the whole
    container out of rotation."""
    return StatusResponse(status="ok")


# (layer name, ingest_log key prefix or None, job name for job_runs-backed layers, interval).
# `intervals` on Settings carries the hour numbers; layers sharing a cadence (land/trails/
# camps/dispersed all ride the weekly `refresh --with ... --all`) share `layers_hours`.
def _layer_specs(cfg: Settings) -> list[tuple[str, str | None, str | None, float]]:
    intervals = cfg.intervals
    return [
        ("observations", "obs:fungi:", None, intervals.ingest_hours),
        ("land", "land:", None, intervals.layers_hours),
        ("trails", "trails:", None, intervals.layers_hours),
        ("camps", "camps:", None, intervals.layers_hours),
        ("dispersed", "dispersed:", None, intervals.layers_hours),
        ("fire", None, "fire", intervals.fire_hours),
        ("precip", None, "refresh-precip", intervals.precip_hours),
    ]


@router.get("/healthz/data")
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
        for name, prefix, job, interval_hours in _layer_specs(cfg):
            last_success = latest_ingest_at(conn, prefix) if prefix else None
            if last_success is None and job:
                run = latest_job_run(conn, job)
                if run is not None and run["status"] == "ok":
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
                )
            )
    ok = not any(layer.stale for layer in layers)
    if not ok:
        response.status_code = 503
    return DataHealthResponse(ok=ok, layers=layers)
