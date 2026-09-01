"""The background ingest-refresh the API server runs in a thread, streaming SSE progress.

This is the API's orchestration of the shared ingest sequence. The CLI (``foray refresh``)
has its own orchestration in ``cli.py`` - printing to stdout, with a coverage-wide ``--all``
mode - and folding the two into one caller is the remaining half of issue #242 Part 1f.
The target vocabulary and month parsing they share already live in ``foray.refresh``.
"""

from __future__ import annotations

import logging
import queue
from typing import Any

import httpx
from psycopg_pool import ConnectionPool

from foray import camps, dispersed, land, scoring, trails
from foray.api.state import AppState
from foray.config import Home, Settings
from foray.ingest import ingest

logger = logging.getLogger(__name__)


def broadcast(state: AppState, msg: dict[str, Any]) -> None:
    state.last_progress = msg
    with state.listeners_lock:
        listener_queues = list(state.listeners)
    for listener_queue in listener_queues:
        try:
            listener_queue.put_nowait(msg)
        except queue.Full:
            try:
                listener_queue.get_nowait()
                listener_queue.put_nowait(msg)
            except (queue.Empty, queue.Full):
                pass


def _make_cb(state: AppState, base_pct: float, range_pct: float) -> Any:
    def cb(step: str, local_pct: float) -> None:
        broadcast(state, {"step": step, "progress": base_pct + range_pct * (local_pct / 100.0)})

    return cb


def run_refresh(state: AppState, pool: ConnectionPool, base_cfg: Settings, home: Home, target: str = "all") -> None:
    # Refresh ingests around *this visitor's* home, not the env-configured default - a
    # per-request Settings with `.home` swapped in lets ingest()/camps.py/land.py/etc. stay
    # unchanged (they all just read `cfg.home` internally).
    refresh_cfg = base_cfg.model_copy(update={"home": home})

    def make_cb(base_pct: float, range_pct: float) -> Any:
        return _make_cb(state, base_pct, range_pct)

    try:
        state.abort_event.clear()
        # 300s covers Overpass trail queries that can take up to 180s; set a
        # generous ceiling so the shared client doesn't cut off slow phases.
        state.http_client = httpx.Client(timeout=300.0)

        broadcast(state, {"step": "Starting refresh…", "progress": 0.0})
        logger.info("refresh: starting for %s (target=%s)", refresh_cfg.home.name, target)

        # One pooled connection checked out for the whole refresh - Postgres handles
        # concurrent readers (other requests borrowing their own connections) natively
        # via MVCC, unlike the DuckDB-era single-writer-file model this replaced.
        with pool.connection() as db:
            if target in ("all", "mushrooms") and not state.abort_event.is_set():
                ingest(
                    refresh_cfg,
                    db,
                    progress_cb=make_cb(0.0, 90.0 if target == "mushrooms" else 50.0),
                    abort_event=state.abort_event,
                )
            if target in ("all", "camps") and not state.abort_event.is_set():
                camps.ingest_campgrounds(
                    refresh_cfg,
                    db,
                    client=state.http_client,
                    progress_cb=make_cb(50.0 if target == "all" else 0.0, 10.0 if target == "all" else 100.0),
                )
            if target in ("all", "land") and not state.abort_event.is_set():
                land.ingest_public_land(
                    refresh_cfg,
                    db,
                    client=state.http_client,
                    progress_cb=make_cb(60.0 if target == "all" else 0.0, 10.0 if target == "all" else 100.0),
                )
            if target in ("all", "dispersed") and not state.abort_event.is_set():
                dispersed.ingest_dispersed(
                    refresh_cfg,
                    db,
                    client=state.http_client,
                    progress_cb=make_cb(70.0 if target == "all" else 0.0, 10.0 if target == "all" else 100.0),
                )
            if target in ("all", "trails") and not state.abort_event.is_set():
                trails.ingest_trails(
                    refresh_cfg,
                    db,
                    client=state.http_client,
                    progress_cb=make_cb(80.0 if target == "all" else 0.0, 10.0 if target == "all" else 100.0),
                )

            if target in ("all", "mushrooms") and not state.abort_event.is_set():
                broadcast(state, {"step": "Building phenology…", "progress": 90.0})
                logger.info("refresh: building phenology…")
                scoring.build_phenology(db, refresh_cfg.cell_deg)

        if state.abort_event.is_set():
            logger.info("refresh: cancelled by user")
            state.last_error = "Cancelled"
            broadcast(state, {"error": "Cancelled", "done": True})
        else:
            state.last_error = None
            broadcast(state, {"step": "Done", "progress": 100.0, "done": True})
            logger.info("refresh: complete")
    except (httpx.LocalProtocolError, httpx.ReadError, httpx.PoolTimeout):
        logger.info("refresh: network client closed explicitly (cancelled)")
        state.last_error = "Cancelled"
        broadcast(state, {"error": "Cancelled", "done": True})
    except Exception as error:  # surface to the UI rather than dying silently
        logger.exception("refresh: failed")
        state.last_error = str(error)
        broadcast(state, {"error": str(error), "done": True})
    finally:
        if state.http_client is not None:
            state.http_client.close()
            state.http_client = None
        state.refreshing = False
