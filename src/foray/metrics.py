"""Prometheus metric collection for `/metrics` (issue #338 PR 1).

A custom `Collector` rather than the `prometheus_client` default global registry - every gauge
here is derived from Postgres (`job_runs`, `backfill_queue`) or the same DB-backed freshness
query `/healthz/data` uses, not from in-process counters, so there is no state to accumulate
between scrapes. `collect()` runs its queries fresh on every call.
"""

from __future__ import annotations

import datetime as dt

import psycopg
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector
from psycopg_pool import ConnectionPool

from foray.cache import backfill_queue_depth, job_run_drain_rate
from foray.config import Settings

# Deferred: foray.api.routes.health imports ForayCollector, so a module-level import here
# would be circular. Both names exist by the time collect() actually runs (well after both
# modules finish importing at app startup).

# One row per (job, status) ever recorded - `job_runs` is never pruned, so these counters are
# monotonic across the process lifetime the way a Prometheus counter is supposed to be (unlike
# a gauge sampled from a rolling window).
_JOB_RUN_STATS_SQL = """
    SELECT job, status, count(*), coalesce(sum(http_429_count), 0)
    FROM job_runs
    GROUP BY job, status
"""

# The most recent successful run per job - what a "how long did the last run take" / "how many
# rows did it move" dashboard panel actually wants, not an average across history.
_LATEST_OK_RUN_SQL = """
    SELECT DISTINCT ON (job) job, duration_ms, rows
    FROM job_runs
    WHERE status = 'ok'
    ORDER BY job, started_at DESC
"""


class ForayCollector(Collector):
    """Registered once per app process (`api/app.py`'s lifespan) against a fresh
    `CollectorRegistry` - the default global registry isn't used since a second `TestClient`
    app in the same test process would otherwise collide on duplicate metric names."""

    def __init__(self, cfg: Settings, pool: ConnectionPool) -> None:
        self._cfg = cfg
        self._pool = pool

    def collect(self):
        with self._pool.connection() as conn:
            yield from self._job_metrics(conn)
            yield from self._backlog_metrics(conn)
            yield from self._layer_metrics(conn)

    def _job_metrics(self, conn: psycopg.Connection):
        runs_total = CounterMetricFamily(
            "foray_job_runs_total",
            "Total foray job_runs rows, by job and outcome (ok/error/skipped).",
            labels=["job", "status"],
        )
        http_429_total = CounterMetricFamily(
            "foray_job_http_429_total",
            "Cumulative HTTP 429 responses recorded across a job's runs.",
            labels=["job"],
        )
        for job, status, count, http_429 in conn.execute(_JOB_RUN_STATS_SQL):
            runs_total.add_metric([job, status], count)
            if http_429:
                http_429_total.add_metric([job], http_429)
        yield runs_total
        yield http_429_total

        last_duration = GaugeMetricFamily(
            "foray_job_last_duration_seconds",
            "Duration of each job's most recent successful run.",
            labels=["job"],
        )
        last_rows = GaugeMetricFamily(
            "foray_job_last_rows",
            "Rows processed by each job's most recent successful run.",
            labels=["job"],
        )
        for job, duration_ms, rows in conn.execute(_LATEST_OK_RUN_SQL):
            # Not every job reports these yet (jobs.py's own docstring: `rows`/`duration_ms`
            # stay NULL for a command that never calls `emit_rows`) - omit rather than emit a
            # misleading 0.
            if duration_ms is not None:
                last_duration.add_metric([job], duration_ms / 1000.0)
            if rows is not None:
                last_rows.add_metric([job], rows)
        yield last_duration
        yield last_rows

    def _backlog_metrics(self, conn: psycopg.Connection):
        from foray.api.routes.health import _BACKLOG_SPECS

        depth = GaugeMetricFamily(
            "foray_backlog_depth", "backfill_queue depth by kind (issue #334 PR 3).", labels=["kind"]
        )
        drain_rate = GaugeMetricFamily(
            "foray_backlog_drain_rate_per_hour",
            "Recent drain rate (rows/hour, averaged over the last 5 successful runs) by kind.",
            labels=["kind"],
        )
        for kind, job in _BACKLOG_SPECS:
            depth.add_metric([kind], backfill_queue_depth(conn, kind))
            rate = job_run_drain_rate(conn, job)
            if rate is not None:
                drain_rate.add_metric([kind], rate)
        yield depth
        yield drain_rate

    def _layer_metrics(self, conn: psycopg.Connection):
        from foray.api.routes.health import compute_layer_freshness

        age = GaugeMetricFamily(
            "foray_layer_age_seconds",
            "Seconds since a layer's last recorded success. Absent if it has never succeeded.",
            labels=["layer"],
        )
        stale = GaugeMetricFamily(
            "foray_layer_stale",
            "1 if the layer is past interval_hours * data_freshness_multiplier, else 0.",
            labels=["layer"],
        )
        now = dt.datetime.now(dt.UTC)
        for layer in compute_layer_freshness(self._cfg, conn, now):
            if layer.last_success is not None:
                last_success = dt.datetime.fromisoformat(layer.last_success)
                age.add_metric([layer.layer], (now - last_success).total_seconds())
            stale.add_metric([layer.layer], 1.0 if layer.stale else 0.0)
        yield age
        yield stale
