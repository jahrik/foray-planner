"""The background ingest-refresh the API server runs in a thread, streaming SSE progress.

The ingest sequence itself lives in :func:`foray.refresh.run_home_refresh`, shared with the
CLI's ``foray refresh``. This module owns only the API-side wrapper: a background thread, a
shared long-timeout HTTP client, cancellation via ``state.abort_event``, and broadcasting
progress to SSE listeners.
"""

from __future__ import annotations

import logging
import queue
from typing import Any

import httpx
from psycopg_pool import ConnectionPool

from foray.api.state import AppState
from foray.config import Home, Settings
from foray.refresh import REFRESH_LAYERS, run_home_refresh

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


def run_refresh(state: AppState, pool: ConnectionPool, base_cfg: Settings, home: Home, target: str = "all") -> None:
    # Refresh ingests around *this visitor's* home, not the env-configured default - a
    # per-request Settings with `.home` swapped in lets ingest()/camps.py/land.py/etc. stay
    # unchanged (they all just read `cfg.home` internally).
    refresh_cfg = base_cfg.model_copy(update={"home": home})
    layers = REFRESH_LAYERS if target == "all" else (target,)

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
            run_home_refresh(
                refresh_cfg,
                db,
                layers,
                client=state.http_client,
                abort_event=state.abort_event,
                progress_cb=lambda step, pct: broadcast(state, {"step": step, "progress": pct}),
            )

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
