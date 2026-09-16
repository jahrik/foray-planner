"""Observations table CRUD, plus the elevation/precip *per-observation* enrichment
queries (``observations.elevation_m``/``precip_7d_mm``/``precip_30d_mm`` columns) -
not the ``precip_daily``/``precipitation`` layer cache, see ``precip_cache.py`` for that."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any, LiteralString

import psycopg

from foray.cache.backfill_queue import dequeue_backfill_batch, refresh_backfill_queue
from foray.cache.core import copy_insert_ignore, copy_upsert
from foray.defaults import H3_RESOLUTION as _DEFAULT_H3_RESOLUTION


def upsert_observations(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Insert observation tuples, backfilling metadata on conflict. Returns rows attempted.

    Every column is refreshed on conflict (not just taxon_id/quality_grade/etc) - a cached
    observation is only ever touched again by a later incremental window or by
    ``ingest.revalidate``, so if a re-fetch happens at all its lat/lng/observed_on/
    positional_accuracy must win too, or a since-corrected location/accuracy on iNat's side
    stays wrong here forever. COALESCE still guards against a partial-loader re-upsert (one
    that doesn't carry every column) blanking out a previously-healed value.
    """
    columns: tuple[LiteralString, ...] = (
        "id",
        "taxon_id",
        "lat",
        "lng",
        "observed_on",
        "month",
        "quality_grade",
        "positional_accuracy",
        "place_guess",
        "uri",
        "obscured",
    )
    return copy_upsert(con, "observations", columns, rows, coalesce=set(columns) - {"id"})


def insert_observations_if_missing(con: psycopg.Connection, rows: Sequence[tuple[Any, ...]]) -> int:
    """Insert observation tuples that aren't already cached; leaves an existing row completely
    untouched. See :func:`copy_insert_ignore` for why the bulk iNat loader
    (``foray.sources.inat_bulk.load_inat``) needs insert-only rather than :func:`upsert_observations`'s
    overwrite semantics. Same column shape/order as ``upsert_observations``. Returns rows
    attempted (not the count actually inserted).
    """
    columns: tuple[LiteralString, ...] = (
        "id",
        "taxon_id",
        "lat",
        "lng",
        "observed_on",
        "month",
        "quality_grade",
        "positional_accuracy",
        "place_guess",
        "uri",
        "obscured",
    )
    return copy_insert_ignore(con, "observations", columns, rows)


def suspect_genus_taxon_ids(con: psycopg.Connection, ratio: float = 3.0) -> list[int]:
    """Genus taxon_ids whose cached observation count has drifted implausibly far above iNat's
    own live count for that genus (``fungi_genera.observations_count``, refreshed weekly by
    ``foray genera-refresh`` - no iNat call needed here, this is DB-only).

    This is the fingerprint of a cross-kingdom name homonym: a fungal genus taxon_id that
    happens to share its scientific name with an established, common, completely unrelated
    animal genus (e.g. fungal *Olla* vs. the ladybug genus *Olla*). A legitimate genus can only
    ever have as many cached rows as fit inside our home-radius/region scoping, which is always
    a fraction of iNat's global total - so "cached far exceeds live" only happens when
    observations of the *other* (non-fungal) taxon got attributed to this taxon_id at ingest
    time and never got re-synced since (see ``ingest.revalidate`` - a live census found 19 such
    genera accounting for ~24k/1.97M cached rows).
    """
    rows = con.execute(
        """
        SELECT o.taxon_id
        FROM observations o
        JOIN fungi_genera g ON g.taxon_id = o.taxon_id
        GROUP BY o.taxon_id, g.observations_count
        HAVING count(*) > %s * COALESCE(g.observations_count, 0)
        """,
        [ratio],
    ).fetchall()
    return [taxon_id for (taxon_id,) in rows]


def observation_ids_for_genus(con: psycopg.Connection, taxon_id: int) -> list[int]:
    rows = con.execute("SELECT id FROM observations WHERE taxon_id = %s", [taxon_id]).fetchall()
    return [row[0] for row in rows]


def delete_observations(con: psycopg.Connection, ids: Sequence[int]) -> int:
    """Remove cached rows by id (``ingest.revalidate`` purging observations no longer Fungi).
    Returns rows attempted (not the actual delete count - ids may already be gone)."""
    if not ids:
        return 0
    con.execute("DELETE FROM observations WHERE id = ANY(%s)", [list(ids)])
    return len(ids)


def stale_observation_ids(con: psycopg.Connection, limit: int) -> list[int]:
    """The next batch for ``ingest.resync``'s full-table grind: never-live-checked rows first
    (``revalidated_at IS NULL`` - every bulk-historical-import row starts this way), then the
    longest-since-checked. Unlike ``suspect_genus_taxon_ids`` (targeted at one known failure
    pattern), this is what eventually re-verifies every column of every cached row against iNat,
    including ``obscured`` (never set by the bulk import) and misidentifications too rare within
    their genus to trip the ratio-based suspect check.
    """
    rows = con.execute(
        "SELECT id FROM observations ORDER BY revalidated_at ASC NULLS FIRST LIMIT %s",
        [limit],
    ).fetchall()
    return [row[0] for row in rows]


def observation_taxon_ids(con: psycopg.Connection, ids: Sequence[int]) -> dict[int, int]:
    """Current cached taxon_id for each id, so a resync/revalidate pass can tell a genuine genus
    reassignment (new taxon_id != this) apart from a same-genus refresh."""
    if not ids:
        return {}
    rows = con.execute("SELECT id, taxon_id FROM observations WHERE id = ANY(%s)", [list(ids)]).fetchall()
    return dict(rows)


def mark_revalidated(con: psycopg.Connection, ids: Sequence[int]) -> None:
    """Stamp ``revalidated_at = now()`` on ids that were just live-checked (whether or not they
    changed) - advances the ``stale_observation_ids`` cursor past them."""
    if not ids:
        return
    con.execute("UPDATE observations SET revalidated_at = now() WHERE id = ANY(%s)", [list(ids)])


def observation_count(con: psycopg.Connection) -> int:
    row = con.execute("SELECT count(*) FROM observations").fetchone()
    return int(row[0]) if row else 0


# Half-width of the bounding box `observations_missing_elevation(near=...)` restricts to: ~1600
# km, comfortably past the home radius and any corridor trip, but small enough that the query
# range-scans ix_observations_lat_lng instead of sorting the whole backlog.
_NEAR_WINDOW_DEG = 15.0


def observations_missing_elevation(
    con: psycopg.Connection,
    limit: int,
    *,
    near: tuple[float, float] | None = None,
    h3_resolution: int = _DEFAULT_H3_RESOLUTION,
) -> list[tuple[int, float, float]]:
    """Up to ``limit`` research-grade observations with in-range coordinates but no elevation
    yet (issue #36). Non-research-grade rows are skipped - scoring only ever reads research-grade
    (scoring._BINNED), so enriching the rest would just burn Open-Meteo quota. Obscured rows are
    skipped too - their cached point is iNat's randomized decoy, so its elevation would be
    meaningless. Out-of-range lat/lng is excluded here so one bad row can't sit at the head of
    the queue and wedge the backfill (``elevation.lookup_batch`` would raise on it every run).

    ``near`` (a ``(lat, lng)``) restricts the queue to a bounding box around that point and
    orders it by squared planar distance, so a Refresh drains the cells the visitor is actually
    looking at first instead of whatever the activity-weighted queue ranks highest nationwide.
    The box (a) keeps Postgres range-scanning ``ix_observations_lat_lng`` instead of sorting the
    whole missing-elevation backlog on every call, and (b) is wide enough (`_NEAR_WINDOW_DEG`,
    ~1600 km) to cover the home radius and any plausible corridor trip - rows further out aren't
    on the visitor's cards anyway. The degree-space distance is a cheap proxy: it only decides
    ordering within the box, and iNat fungal data is effectively all mid-latitude. A box that
    straddles the antimeridian is split into its two wrapped longitude ranges.

    Without ``near`` (the hourly prod cron's whole-backlog drain), rows come from
    ``backfill_queue`` instead of a plain scan - see :func:`refresh_backfill_queue` for the
    activity-weighted priority behind that (issue #334 PR 3) and ``h3_resolution`` for its region
    binning."""
    if near is not None:
        plat, plng = near
        lo_lng, hi_lng = plng - _NEAR_WINDOW_DEG, plng + _NEAR_WINDOW_DEG
        box_params: list[float] = [plat - _NEAR_WINDOW_DEG, plat + _NEAR_WINDOW_DEG]
        if lo_lng < -180.0 or hi_lng > 180.0:
            # The box straddles the antimeridian (a visitor in the western Aleutians). Split the
            # longitude test into its two wrapped ranges so Postgres still range-scans
            # ix_observations_lat_lng instead of matching nothing on the out-of-range bound.
            box_sql: LiteralString = (
                " AND lat BETWEEN %s AND %s AND (lng BETWEEN %s AND 180 OR lng BETWEEN -180 AND %s) "
            )
            box_params += [(lo_lng + 180.0) % 360.0 - 180.0, (hi_lng + 180.0) % 360.0 - 180.0]
        else:
            box_sql = " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s "
            box_params += [lo_lng, hi_lng]
        # Wrap the longitude delta too, so a point just across the dateline sorts as near, not
        # ~360 deg away.
        order_sql: LiteralString = (
            "ORDER BY (lat - %s) * (lat - %s) + power(LEAST(ABS(lng - %s), 360.0 - ABS(lng - %s)), 2)"
        )
        order_params: list[float] = [plat, plat, plng, plng]
        rows = con.execute(
            """
            SELECT id, lat, lng FROM observations
            WHERE elevation_m IS NULL
                  AND lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180
                  AND quality_grade = 'research'
                  AND NOT COALESCE(obscured, false)
            """
            + box_sql
            + order_sql
            + " LIMIT %s",
            [*box_params, *order_params, limit],
        ).fetchall()
        return [(int(obs_id), float(lat), float(lng)) for obs_id, lat, lng in rows]
    refresh_backfill_queue(con, "elevation", h3_resolution)
    obs_ids = dequeue_backfill_batch(con, "elevation", limit)
    if not obs_ids:
        return []
    # unnest(...) WITH ORDINALITY + ORDER BY, not `id = ANY(%s)` - the latter doesn't preserve
    # the input array's order, which would silently discard dequeue_backfill_batch's priority
    # ranking before the caller ever sees it (Copilot review, PR #351).
    rows = con.execute(
        "SELECT o.id, o.lat, o.lng FROM unnest(%s::bigint[]) WITH ORDINALITY AS t(id, ord) "
        "JOIN observations o ON o.id = t.id ORDER BY t.ord",
        [obs_ids],
    ).fetchall()
    return [(int(obs_id), float(lat), float(lng)) for obs_id, lat, lng in rows]


def set_observation_elevations(con: psycopg.Connection, rows: Sequence[tuple[int, int]]) -> int:
    """Write looked-up elevations back. ``rows`` is ``(observation_id, elevation_m)`` and carries
    only rows that got a real value - a point Open-Meteo had no answer for is left NULL and
    retried on the next backfill (a healthy response always has a value, even 0 for open sea,
    so this does not loop in practice)."""
    if not rows:
        return 0
    with con.cursor() as cur:
        cur.executemany(
            "UPDATE observations SET elevation_m = %s WHERE id = %s", [(elev, obs_id) for obs_id, elev in rows]
        )
    return len(rows)


def observations_missing_precip(
    con: psycopg.Connection,
    limit: int,
    *,
    near: tuple[float, float] | None = None,
    h3_resolution: int = _DEFAULT_H3_RESOLUTION,
) -> list[tuple[int, float, float, dt.date]]:
    """Up to ``limit`` research-grade, non-obscured observations with coordinates and an
    ``observed_on`` but at least one of ``precip_7d_mm`` / ``precip_30d_mm`` still unset
    (issue #226). Same research-grade/obscured/in-range filters as
    :func:`observations_missing_elevation`; ordered by ``near`` (planar distance) when given.

    ``precip_7d_mm IS NULL OR precip_30d_mm IS NULL`` is the pending sentinel: a row whose 7 d
    sum lands but whose 30 d sum still touches an ERA5-null day is written partially (7 d only)
    and reappears here next pass so the 30 d column gets retried too.

    Observations dated before ERA5's coverage (1940) are excluded - Open-Meteo's archive has no
    data for them and, left in, one such row 400s the archive request for its whole grid cell
    and wedges the backfill (same reasoning as the lat/lng-range filter).

    Without ``near`` (the hourly prod cron's whole-backlog drain), rows come from the
    activity-weighted ``backfill_queue`` instead of oldest-``observed_on``-first - see
    :func:`observations_missing_elevation`'s matching note (issue #334 PR 3). ``backfill_precip``
    re-sorts each cell's members by ``observed_on`` itself once grouped, so the queue's return
    order not being date-sorted doesn't matter here."""
    if near is not None:
        plat, plng = near
        box_sql: LiteralString = " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s "
        params: list[Any] = [
            plat - _NEAR_WINDOW_DEG,
            plat + _NEAR_WINDOW_DEG,
            plng - _NEAR_WINDOW_DEG,
            plng + _NEAR_WINDOW_DEG,
        ]
        order_sql: LiteralString = "ORDER BY (lat - %s) * (lat - %s) + (lng - %s) * (lng - %s)"
        params += [plat, plat, plng, plng]
        rows = con.execute(
            """
            SELECT id, lat, lng, observed_on FROM observations
            WHERE (precip_7d_mm IS NULL OR precip_30d_mm IS NULL)
                  AND observed_on >= DATE '1940-02-01'
                  AND lat BETWEEN -90 AND 90 AND lng BETWEEN -180 AND 180
                  AND quality_grade = 'research'
                  AND NOT COALESCE(obscured, false)
            """
            + box_sql
            + order_sql
            + " LIMIT %s",
            [*params, limit],
        ).fetchall()
        return [(int(obs_id), float(lat), float(lng), observed_on) for obs_id, lat, lng, observed_on in rows]
    refresh_backfill_queue(con, "precip", h3_resolution)
    obs_ids = dequeue_backfill_batch(con, "precip", limit)
    if not obs_ids:
        return []
    # unnest(...) WITH ORDINALITY, not `id = ANY(%s)` - see the matching note in
    # observations_missing_elevation.
    rows = con.execute(
        "SELECT o.id, o.lat, o.lng, o.observed_on FROM unnest(%s::bigint[]) WITH ORDINALITY AS t(id, ord) "
        "JOIN observations o ON o.id = t.id ORDER BY t.ord",
        [obs_ids],
    ).fetchall()
    return [(int(obs_id), float(lat), float(lng), observed_on) for obs_id, lat, lng, observed_on in rows]


def set_observation_precip(con: psycopg.Connection, rows: Sequence[tuple[int, float | None, float | None]]) -> int:
    """Write ``(observation_id, precip_7d_mm, precip_30d_mm)`` back. Callers pass only rows whose
    7 d window was fully covered - a row still missing an ERA5 day is left NULL (pending)."""
    if not rows:
        return 0
    with con.cursor() as cur:
        cur.executemany(
            "UPDATE observations SET precip_7d_mm = %s, precip_30d_mm = %s WHERE id = %s",
            [(mm7, mm30, obs_id) for obs_id, mm7, mm30 in rows],
        )
    return len(rows)
