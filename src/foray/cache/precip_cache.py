"""The ``precip_daily`` (per grid-cell-day) and ``precipitation`` (per-region trailing-
window) layer caches (issue #226). Not the *per-observation* ``precip_7d_mm``/
``precip_30d_mm`` columns - see ``observations.py`` for those."""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Mapping, Sequence
from typing import Any, LiteralString

import psycopg

# --- Precipitation cache (issue #226) -------------------------------------------------------


def cached_precip(
    con: psycopg.Connection, cell_id: str, start: dt.date, end: dt.date, *, source: str | None = None
) -> dict[dt.date, float | None]:
    """``date -> precip_mm`` already cached for ``cell_id`` in ``[start, end]`` (inclusive).

    A day absent from the result was never fetched; a day present with value ``None`` is one
    Open-Meteo returned null for (ERA5 lag). ``backfill_precip`` treats both the same - "not
    known yet", so it refetches the span - and never records a window sum that touches such a
    day. ``source`` restricts to one origin: the per-observation backfill trusts only ERA5
    archive rows, never the provisional forecast rows the layer refresh also writes."""
    query: LiteralString = "SELECT date, precip_mm FROM precip_daily WHERE cell_id = %s AND date BETWEEN %s AND %s"
    params: list[Any] = [cell_id, start, end]
    if source is not None:
        query += " AND source = %s"
        params.append(source)
    rows = con.execute(query, params).fetchall()
    return {day: (float(mm) if mm is not None else None) for day, mm in rows}


def upsert_precip_days(con: psycopg.Connection, cell_id: str, days: Mapping[dt.date, float | None], source: str) -> int:
    """Cache per-day precipitation for one grid cell. Overwrites an existing ``(cell_id, date)``
    row so a later archive pull can replace a forecast estimate (or fill a previously-null day).
    Returns rows written."""
    if not days:
        return 0
    now = dt.datetime.now(dt.UTC)
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO precip_daily (cell_id, date, precip_mm, source, fetched_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (cell_id, date) DO UPDATE SET
                precip_mm = EXCLUDED.precip_mm, source = EXCLUDED.source, fetched_at = EXCLUDED.fetched_at
            """,
            [(cell_id, day, mm, source, now) for day, mm in days.items()],
        )
    return len(days)


def stale_precip_region_ids(con: psycopg.Connection, older_than_hours: float) -> list[str]:
    """Active region cells whose ``precipitation`` row is missing or older than
    ``older_than_hours`` (issue #226 Part 2). Lets the layer refresh skip cells done recently, so
    a re-run - or one resumed after the scheduler restarted mid-pass - continues instead of
    starting from scratch. Empty when ``regions`` doesn't exist yet."""
    try:
        rows = con.execute(
            """
            SELECT r.region_id
            FROM regions r
            LEFT JOIN precipitation p ON p.region_id = r.region_id
            WHERE p.region_id IS NULL
               OR p.updated_at IS NULL
               OR p.updated_at < now() - make_interval(hours => %s)
            """,
            [older_than_hours],
        ).fetchall()
    except psycopg.errors.UndefinedTable:
        con.rollback()
        return []
    return [region_id for (region_id,) in rows]


def region_precip(con: psycopg.Connection, region_ids: Collection[str]) -> dict[str, dict[str, float | None]]:
    """``region_id -> {"precip_7d_mm", "precip_14d_mm", "precip_30d_mm"}`` from the
    ``precipitation`` layer table (issue #226). Absent when that cell has never been refreshed."""
    if not region_ids:
        return {}
    try:
        rows = con.execute(
            "SELECT region_id, precip_7d_mm, precip_14d_mm, precip_30d_mm FROM precipitation WHERE region_id = ANY(%s)",
            [list(region_ids)],
        ).fetchall()
    except psycopg.errors.UndefinedTable:
        con.rollback()
        return {}
    return {
        region_id: {"precip_7d_mm": mm7, "precip_14d_mm": mm14, "precip_30d_mm": mm30}
        for region_id, mm7, mm14, mm30 in rows
    }


def upsert_region_precip(
    con: psycopg.Connection, rows: Sequence[tuple[str, float | None, float | None, float | None]]
) -> int:
    """Upsert ``(region_id, precip_7d_mm, precip_14d_mm, precip_30d_mm)`` into ``precipitation``."""
    if not rows:
        return 0
    now = dt.datetime.now(dt.UTC)
    with con.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO precipitation (region_id, precip_7d_mm, precip_14d_mm, precip_30d_mm, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (region_id) DO UPDATE SET
                precip_7d_mm = EXCLUDED.precip_7d_mm, precip_14d_mm = EXCLUDED.precip_14d_mm,
                precip_30d_mm = EXCLUDED.precip_30d_mm, updated_at = EXCLUDED.updated_at
            """,
            [(region_id, mm7, mm14, mm30, now) for region_id, mm7, mm14, mm30 in rows],
        )
    return len(rows)
