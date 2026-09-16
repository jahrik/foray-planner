"""The ``ingest_log`` coverage-tracking table + ``job_runs`` bookkeeping - "has this
area/window already been fetched" and "when did this scheduled job last run"."""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg

from foray.geo import haversine_km


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


def record_job_run(
    con: psycopg.Connection,
    job: str,
    *,
    started_at: dt.datetime,
    ended_at: dt.datetime,
    status: str,
    rows: int | None = None,
    duration_ms: int | None = None,
    http_429_count: int = 0,
) -> None:
    """One ``job_runs`` row per scheduled-job attempt (issue #332), written by
    ``foray.jobs.run`` regardless of outcome - ``status`` is ``"ok"``, ``"error"``, or
    ``"skipped"`` (an overlapping run found the advisory lock already held)."""
    con.execute(
        "INSERT INTO job_runs (job, started_at, ended_at, status, rows, duration_ms, http_429_count) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        [job, started_at, ended_at, status, rows, duration_ms, http_429_count],
    )


def latest_job_run(con: psycopg.Connection, job: str) -> dict[str, Any] | None:
    """Most recent ``job_runs`` row for ``job`` regardless of outcome, or ``None`` if it has
    never run. For the freshness signal, use ``latest_successful_job_run`` instead - this one
    exists for callers that want to know the outcome of the last *attempt* (e.g. `foray job`
    itself), not the last success."""
    row = con.execute(
        "SELECT status, started_at, ended_at FROM job_runs WHERE job = %s ORDER BY started_at DESC LIMIT 1",
        [job],
    ).fetchone()
    if row is None:
        return None
    status, started_at, ended_at = row
    return {"status": status, "started_at": started_at, "ended_at": ended_at}


def latest_successful_job_run(con: psycopg.Connection, job: str) -> dict[str, Any] | None:
    """Most recent ``status = 'ok'`` ``job_runs`` row for ``job``, or ``None`` if it has never
    succeeded - the freshness signal ``/healthz/data`` uses for layers with no ``ingest_log``
    marker of their own (fire's replace-semantics refresh, the recent-rain-per-destination
    layer). Filtering by status here (rather than fetching the latest row and checking it)
    means a failed/skipped retry that runs after a still-fresh success doesn't make the layer
    look stale."""
    row = con.execute(
        "SELECT started_at, ended_at FROM job_runs WHERE job = %s AND status = 'ok' ORDER BY started_at DESC LIMIT 1",
        [job],
    ).fetchone()
    if row is None:
        return None
    started_at, ended_at = row
    return {"status": "ok", "started_at": started_at, "ended_at": ended_at}


def is_ingested(con: psycopg.Connection, key: str) -> bool:
    row = con.execute("SELECT 1 FROM ingest_log WHERE key = %s", [key]).fetchone()
    return row is not None


def forget_ingest(con: psycopg.Connection, key: str) -> int:
    """Drop one ``ingest_log`` key so a one-shot ingest re-runs on its next pass. Returns the
    number of rows removed (0 or 1). The key must be the exact stored value (e.g.
    ``trails:place:12345:q2``, the versioned marker ``ingest_trails_region`` writes) - this does
    no prefix matching. Used by ``foray trails --force`` to re-pull a single coverage region."""
    result = con.execute("DELETE FROM ingest_log WHERE key = %s", [key])
    return result.rowcount


def latest_ingest_at(con: psycopg.Connection, prefix: str) -> dt.datetime | None:
    """Newest ``ingest_log.fetched_at`` across every key starting with ``prefix`` - the
    generalization of ``/api/coverage``'s per-region latest-ingest query
    (``api.routes.coverage``) that ``/healthz/data`` (``api.routes.health``) reuses for the
    other layers (land, trails, camps, dispersed) that also mark their ingests here."""
    row = con.execute("SELECT max(fetched_at) FROM ingest_log WHERE key LIKE %s", [f"{prefix}%"]).fetchone()
    return row[0] if row else None


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
