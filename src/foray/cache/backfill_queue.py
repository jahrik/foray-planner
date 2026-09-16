"""Activity-weighted backfill priority queue (issue #334 PR 3) - lets the elevation/
precip enrichment crons drain the cell a visitor is actually looking at first, instead
of strict oldest-first order. See ``observations.py`` for the callers."""

from __future__ import annotations

import logging
from typing import LiteralString

import psycopg

logger = logging.getLogger(__name__)

# Same research-grade/non-obscured/in-range filters `observations_missing_elevation` and
# `observations_missing_precip` already apply, factored out so `refresh_backfill_queue` stays
# byte-for-byte in sync with what those functions consider eligible - a divergence here would
# either queue rows that can never be drained (they'd need a `near` call to touch them) or,
# worse, silently exclude rows a caller expects the cron sweep to reach.
_BACKFILL_ELIGIBLE: dict[str, LiteralString] = {
    "elevation": (
        "elevation_m IS NULL AND lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180 "
        "AND quality_grade = 'research' AND NOT COALESCE(obscured, false)"
    ),
    "precip": (
        "(precip_7d_mm IS NULL OR precip_30d_mm IS NULL) AND observed_on >= DATE '1940-02-01' "
        "AND lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180 "
        "AND quality_grade = 'research' AND NOT COALESCE(obscured, false)"
    ),
}

# How far back "recent activity" looks when scoring a grid cell's priority - roughly a season,
# long enough that a cell with a real active community of observers doesn't look dormant
# between visits, short enough that a cell nobody has photographed in years sinks to the
# bottom rather than coasting on activity from long ago.
_BACKFILL_ACTIVITY_WINDOW_DAYS = 180


def refresh_backfill_queue(con: psycopg.Connection, kind: str, h3_resolution: int) -> int:
    """(Re)populate ``backfill_queue`` for ``kind`` (``"elevation"`` or ``"precip"``) from
    ``observations`` - the priority behind issue #334 PR 3's "prioritize backfill by region
    activity, not strict staleness order" (TODO.md E4).

    Re-derived from scratch each call rather than incrementally maintained (enqueue-on-ingest,
    dequeue-on-enrich): every ingest/bulk-load/revalidate/resync path would otherwise need to
    remember to keep the queue in sync, and one that forgot would silently leave rows stuck.
    Instead this is the same self-healing shape ``scoring.regions.build_phenology`` and the
    coverage-wide ingests already use - re-derive from the source of truth every time, so a row
    enriched some other way (a live Refresh's ``near`` path, ``backfill-elevation-dem``), no
    longer eligible (revalidate found it non-research), or deleted outright simply stops
    reappearing, no separate cleanup pass needed. Cost is bounded by the *backlog* size, not the
    whole table, via the same partial indexes (``ix_observations_elevation_missing`` /
    ``ix_observations_precip_missing``) the un-queued query used - only the activity-scoring
    join scans a wider (but time-bounded) window.

    Priority is the count of research-grade observations in the same H3 cell (``h3_resolution``,
    matching ``regions``/``phenology``) observed within the last
    ``_BACKFILL_ACTIVITY_WINDOW_DAYS`` days - a cell with recent activity outranks one that has
    been quiet, regardless of which specific row is older. Returns the number of rows now
    queued for ``kind``.
    """
    if kind not in _BACKFILL_ELIGIBLE:
        raise ValueError(f"unknown backfill kind {kind!r} (expected one of {sorted(_BACKFILL_ELIGIBLE)})")
    eligible_sql = _BACKFILL_ELIGIBLE[kind]
    # A literal (no interpolation) - h3_resolution rides through as a bound %s param below instead
    # (Copilot review, PR #351: f-string-interpolating a float into SQL text works but invites
    # exactly this kind of question; parameterizing removes the doubt for free here).
    cell_sql: LiteralString = "h3_lat_lng_to_cell(POINT(lng, lat), %s)::text"
    con.execute(
        f"""
        WITH eligible AS (
            SELECT id, {cell_sql} AS region_id FROM observations WHERE {eligible_sql}
        ),
        activity AS (
            SELECT {cell_sql} AS region_id, count(*) AS recent_count
            FROM observations
            WHERE quality_grade = 'research' AND observed_on >= (CURRENT_DATE - %s * INTERVAL '1 day')
                  AND lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180
                  AND NOT COALESCE(obscured, false)
            GROUP BY 1
        )
        INSERT INTO backfill_queue (kind, obs_id, priority)
        SELECT %s, e.id, COALESCE(a.recent_count, 0)
        FROM eligible e LEFT JOIN activity a USING (region_id)
        ON CONFLICT (kind, obs_id) DO UPDATE SET priority = EXCLUDED.priority
        """,
        # Matches placeholder order left-to-right: eligible's cell_sql (resolution), activity's
        # cell_sql (resolution), the activity window, then the INSERT's `kind` literal.
        [h3_resolution, h3_resolution, _BACKFILL_ACTIVITY_WINDOW_DAYS, kind],
    )
    result = con.execute(
        f"""
        DELETE FROM backfill_queue q
        WHERE q.kind = %s
          AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.id = q.obs_id AND {eligible_sql})
        """,
        [kind],
    )
    if result.rowcount:
        logger.info("backfill_queue: dropped %d %s row(s) no longer eligible", result.rowcount, kind)
    return backfill_queue_depth(con, kind)


def backfill_queue_depth(con: psycopg.Connection, kind: str) -> int:
    """Current ``backfill_queue`` row count for ``kind`` - the "backlog" gauge for
    ``/healthz/backlog`` and the CLI's own progress output."""
    row = con.execute("SELECT count(*) FROM backfill_queue WHERE kind = %s", [kind]).fetchone()
    return row[0] if row else 0


def dequeue_backfill_batch(con: psycopg.Connection, kind: str, limit: int) -> list[int]:
    """Claim (and remove) up to ``limit`` observation ids for ``kind``, highest priority first.

    ``FOR UPDATE SKIP LOCKED`` makes concurrent claims (an inline post-ingest top-up racing the
    scheduled drain) safe without blocking each other - a row already claimed by one caller is
    simply invisible to the other, not waited on. Removing on claim (rather than marking
    ``claimed_at``) needs no separate "stale claim" recovery: a row this call fails to enrich
    (the caller's HTTP call errors) just gets re-queued on the next ``refresh_backfill_queue``
    pass, since it's still eligible in ``observations``.

    The returned list is in priority order (highest first) - ``DELETE ... RETURNING`` doesn't
    itself guarantee that (a `CTE`'s `ORDER BY`/`LIMIT` shapes *which* rows are matched, not the
    order the final statement returns them in), so the claimed rows carry an explicit rank
    (`row_number()`) and are re-sorted by it in Python after the round-trip (Copilot review,
    PR #351).
    """
    if limit <= 0:
        return []
    rows = con.execute(
        """
        WITH candidates AS (
            -- FOR UPDATE can't share a CTE with a window function, so the lock+limit
            -- happens here and the row_number() ranking happens in a separate CTE over
            -- this already-small, already-locked result.
            SELECT obs_id, priority, enqueued_at FROM backfill_queue
            WHERE kind = %s
            ORDER BY priority DESC, enqueued_at ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        ),
        claimed AS (
            SELECT obs_id, row_number() OVER (ORDER BY priority DESC, enqueued_at ASC) AS rank
            FROM candidates
        )
        DELETE FROM backfill_queue q USING claimed c
        WHERE q.kind = %s AND q.obs_id = c.obs_id
        RETURNING q.obs_id, c.rank
        """,
        [kind, limit, kind],
    ).fetchall()
    rows.sort(key=lambda row: row[1])
    return [obs_id for obs_id, _rank in rows]


def job_run_drain_rate(con: psycopg.Connection, job: str, *, sample_runs: int = 5) -> float | None:
    """Average rows/hour across the last ``sample_runs`` successful ``job_runs`` for ``job`` -
    the "drain rate" half of issue #334 PR 3's backlog/drain-rate exposure (``/healthz/backlog``).
    ``None`` if ``job`` has never succeeded, or every sampled run recorded no duration (can't
    compute a rate)."""
    rows = con.execute(
        "SELECT rows, duration_ms FROM job_runs WHERE job = %s AND status = 'ok' "
        "AND rows IS NOT NULL AND duration_ms > 0 ORDER BY started_at DESC LIMIT %s",
        [job, sample_runs],
    ).fetchall()
    if not rows:
        return None
    total_rows = sum(r for r, _ in rows)
    total_ms = sum(ms for _, ms in rows)
    if total_ms <= 0:
        return None
    return total_rows / (total_ms / 3_600_000.0)
