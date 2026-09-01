"""``/api/coverage`` - each configured region with its latest ingest timestamp."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from psycopg_pool import ConnectionPool

from foray.api.deps import get_pool, get_state
from foray.api.state import AppState
from foray.api_models import CoverageRegionResponse

router = APIRouter()


@router.get("/api/coverage")
def get_coverage(
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> list[CoverageRegionResponse]:
    """Coverage regions with their latest ingest timestamps."""
    cfg = state.cfg
    with pool.connection() as conn:
        # Since issue #79 Phase 4, ingest_region() writes one whole-Fungi-kingdom
        # ingest_log row per (place, window) rather than one per taxon, and each
        # incremental run's window overlaps the previous one by 7 days - summing
        # row_count across every historical row would double-count that overlap (and
        # pull in pre-Phase-4 per-taxon keys on an already-deployed database). The
        # latest run's own row_count is what "observations ingested" should mean.
        # One aggregate query for every region (issue #94) instead of a round trip per
        # region - cfg.coverage defaults to 50 US states, so this collapses 50 queries
        # into 1.
        rows = conn.execute(
            """
            SELECT DISTINCT ON (split_part(key, ':', 4))
                split_part(key, ':', 4)::int AS place_id, fetched_at, row_count
            FROM ingest_log
            WHERE key LIKE 'obs:fungi:place:%'
            ORDER BY split_part(key, ':', 4), fetched_at DESC
            """
        ).fetchall()
        by_place_id = {row[0]: (row[1], row[2]) for row in rows}
    results = []
    for region in cfg.coverage:
        latest = by_place_id.get(region.place_id)
        results.append(
            CoverageRegionResponse(
                name=region.name,
                place_id=region.place_id,
                last_ingest=latest[0].isoformat() if latest else None,
                observations_ingested=latest[1] if latest else 0,
            )
        )
    return results
