"""Wildfire perimeter/point table writes + MTBS/RAVG burn-severity enrichment (issue #227)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, LiteralString

import psycopg

from foray.cache.core import _invalidate_rank_cache, upsert_rows

_FIRE_COLUMNS: tuple[LiteralString, ...] = (
    "id",
    "source_key",
    "feature_id",
    "irwin_id",
    "name",
    "status",
    "fire_year",
    "discovery_date",
    "percent_contained",
    "gis_acres",
    "incident_url",
    "is_point",
    "center_lat",
    "center_lng",
    "geojson",
    "fetched_at",
)


def upsert_fire_perimeters(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert fire perimeter / point rows (issue #227), refreshing existing rows in place.

    Tuple order is :data:`_FIRE_COLUMNS`. The MTBS severity columns are written separately by
    :func:`apply_fire_severity` (backfill-style), so a plain refresh never blanks them."""
    result = upsert_rows(con, "fire_perimeters", _FIRE_COLUMNS, rows)
    _invalidate_rank_cache()
    return result


def replace_fire_lane(con: psycopg.Connection, source_key: str, rows: Sequence[tuple[Any, ...]]) -> int:
    """Upsert ``rows`` for ``source_key`` and delete any existing row in that lane no longer
    present in the source (issue #227's active lane - "a contained fire drops out").

    Wrapped in one transaction so a reader never sees the lane mid-swap. Returns rows upserted.
    An empty ``rows`` with no prior data is a no-op; an empty ``rows`` after the source
    legitimately reports zero active fires clears the lane.

    Calls ``upsert_rows`` directly rather than :func:`upsert_fire_perimeters` - that wrapper's
    own ``_invalidate_rank_cache()`` call would fire *inside* this transaction, before the
    ``with`` block's implicit commit. A concurrent ranking call in that window would still read
    the pre-replace lane (transaction isolation), recompute, and cache that stale result under
    the generation this invalidate already bumped - nothing would ever correct it (Copilot
    review, PR #348). Invalidating once, after the transaction actually commits, closes that.
    """
    keep_ids = [row[0] for row in rows]
    with con.transaction():
        if keep_ids:
            con.execute(
                "DELETE FROM fire_perimeters WHERE source_key = %s AND id <> ALL(%s)",
                [source_key, keep_ids],
            )
        else:
            con.execute("DELETE FROM fire_perimeters WHERE source_key = %s", [source_key])
        upsert_rows(con, "fire_perimeters", _FIRE_COLUMNS, rows)
    _invalidate_rank_cache()
    return len(rows)


_MTBS_UPDATE: dict[str, LiteralString] = {
    "irwin_id": (
        "UPDATE fire_perimeters SET severity_unburned_acres = %s, severity_low_acres = %s, "
        "severity_moderate_acres = %s, severity_high_acres = %s, dominant_severity = %s, "
        "mtbs_fire_id = COALESCE(%s, mtbs_fire_id) WHERE irwin_id = %s"
    ),
    "mtbs_fire_id": (
        "UPDATE fire_perimeters SET severity_unburned_acres = %s, severity_low_acres = %s, "
        "severity_moderate_acres = %s, severity_high_acres = %s, dominant_severity = %s, "
        "mtbs_fire_id = COALESCE(%s, mtbs_fire_id) WHERE mtbs_fire_id = %s"
    ),
    # issue #335 PR 4 (RAVG): no shared id field exists between RAVG's export and our
    # WFIGS-derived rows (checked live - see foray.sources.ravg's module docstring), so this
    # source enriches by normalized name + year instead. `match_value` is a `(name, year)` pair,
    # not a scalar - `apply_fire_severity` branches on that below rather than this dict alone.
    # Name + year alone isn't a unique key (two unrelated fires nationwide can share a common
    # name like "Bear" in the same year - Copilot review, PR #366), so the WHERE clause also
    # requires every currently-matching row to resolve to the same underlying fire -
    # `COALESCE(irwin_id, id)` groups the active/history-lane duplicate rows a single real fire
    # legitimately has (they share `irwin_id`) while still telling two genuinely different fires
    # apart (different `irwin_id`, or both NULL and therefore different `id`). Ambiguous matches
    # are skipped entirely rather than guessed at.
    "name_year": (
        "UPDATE fire_perimeters SET severity_unburned_acres = %s, severity_low_acres = %s, "
        "severity_moderate_acres = %s, severity_high_acres = %s, dominant_severity = %s, "
        "mtbs_fire_id = COALESCE(%s, mtbs_fire_id) "
        "WHERE upper(regexp_replace(name, '\\s+FIRE$', '', 'i')) = %s AND fire_year = %s "
        "AND (SELECT count(DISTINCT COALESCE(f2.irwin_id, f2.id)) FROM fire_perimeters f2 "
        "WHERE upper(regexp_replace(f2.name, '\\s+FIRE$', '', 'i')) = %s AND f2.fire_year = %s) = 1"
    ),
}


def apply_fire_severity(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Write burn-severity enrichment onto existing fire rows (issue #227, extended by #335 PR 4),
    matched by ``irwin_id``, ``mtbs_fire_id``, or (RAVG) a normalized ``name_year`` pair. Tuple:
    ``(match_key, match_value, unburned, low, moderate, high, dominant, mtbs_fire_id)`` -
    ``match_value`` is a scalar id for ``"irwin_id"``/``"mtbs_fire_id"``, or a
    ``(normalized_name, fire_year)`` pair for ``"name_year"``. Returns the total number of
    perimeter rows updated (a single source row can match more than one, e.g. the same fire
    present in both the active and history lanes during the brief overlap window)."""
    updated = 0
    for match_key, match_value, unburned, low, moderate, high, dominant, mtbs_fire_id in rows:
        if match_key not in _MTBS_UPDATE:
            raise ValueError(f"bad MTBS match key: {match_key!r}")
        if match_key == "name_year":
            name, year = match_value
            if not name or year is None:
                continue
            params = [unburned, low, moderate, high, dominant, mtbs_fire_id, name, year, name, year]
        else:
            if match_value in (None, ""):
                continue
            params = [unburned, low, moderate, high, dominant, mtbs_fire_id, match_value]
        result = con.execute(_MTBS_UPDATE[match_key], params)
        updated += result.rowcount or 0
    if updated:
        _invalidate_rank_cache()
    return updated
