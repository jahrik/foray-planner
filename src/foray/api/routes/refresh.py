"""``/api/refresh`` - trigger, cancel, and stream progress of a background ingest refresh."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from psycopg_pool import ConnectionPool

from foray.api.deps import (
    check_refresh_rate_limit,
    get_pool,
    get_state,
    resolve_device_id,
    resolve_home,
    set_device_cookie,
)
from foray.api.refresh_runner import run_refresh
from foray.api.security import client_ip
from foray.api.state import AppState
from foray.api_models import StatusResponse
from foray.refresh import REFRESH_TARGETS

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/refresh")
def refresh(
    request: Request,
    response: Response,
    target: str = Query("mushrooms"),
    state: AppState = Depends(get_state),
    pool: ConnectionPool = Depends(get_pool),
) -> StatusResponse:
    if target not in REFRESH_TARGETS:
        raise HTTPException(400, f"unknown target '{target}'; valid: {sorted(REFRESH_TARGETS)}")
    # Check-and-set must be one atomic step, else two concurrent requests can both see
    # `refreshing=False` and both start a refresh thread.
    with state.refresh_lock:
        if state.refreshing:
            return StatusResponse(status="already running")
        state.refreshing = True
    try:
        check_refresh_rate_limit(state, client_ip(request))
        device_id, is_new = resolve_device_id(request)
        if is_new:
            set_device_cookie(request, response, device_id)
        with pool.connection() as conn:
            home = resolve_home(conn, device_id, state.cfg)
    except Exception:
        state.refreshing = False
        raise
    state.last_error = None
    state.last_progress = None
    threading.Thread(target=run_refresh, args=(state, pool, state.cfg, home, target), daemon=True).start()
    return StatusResponse(status="started")


@router.delete("/api/refresh")
def cancel_refresh(state: AppState = Depends(get_state)) -> StatusResponse:
    if state.refreshing:
        state.abort_event.set()
        if state.http_client is not None:
            state.http_client.close()
        return StatusResponse(status="cancelling")
    return StatusResponse(status="idle")


@router.get(
    "/api/refresh/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Server-sent progress events",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def refresh_stream(state: AppState = Depends(get_state)) -> StreamingResponse:
    listener_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)
    if state.last_progress:
        listener_queue.put_nowait(state.last_progress)
    with state.listeners_lock:
        state.listeners.append(listener_queue)

    async def event_generator():
        try:
            while True:
                try:
                    msg = await asyncio.to_thread(listener_queue.get, True, 0.5)
                except queue.Empty:
                    continue
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("done") or msg.get("error"):
                    break
        finally:
            with state.listeners_lock:
                if listener_queue in state.listeners:
                    state.listeners.remove(listener_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
