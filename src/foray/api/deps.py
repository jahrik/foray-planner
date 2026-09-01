"""Shared request helpers: app-state accessors, anonymous device identity, param parsing.

These were nested functions inside ``create_app`` closing over ``state``/``pool``; pulling
them out to module scope is what lets the route modules (and unit tests) reach them.
"""

from __future__ import annotations

import re
import secrets
import time

import psycopg
from fastapi import HTTPException, Request, Response
from psycopg_pool import ConnectionPool

from foray.api.security import is_https
from foray.api.state import AppState
from foray.cache import load_genera as db_load_genera
from foray.cache import load_location as db_load_location
from foray.config import Home, Settings
from foray.refresh import parse_month_list


def get_state(request: Request) -> AppState:
    """The one :class:`AppState` for this app, stashed on ``app.state`` by ``create_app``."""
    return request.app.state.foray


def get_pool(request: Request) -> ConnectionPool:
    """The Postgres connection pool, stashed on ``app.state`` by ``create_app``."""
    return request.app.state.pool


_DEVICE_ID_COOKIE = "device_id"
_DEVICE_ID_MAX_AGE = 60 * 60 * 24 * 365  # ~1 year
# Matches secrets.token_urlsafe's output alphabet; bounds reject junk a client could send
# in a hand-crafted cookie (log/DB-key bloat) without hard-coding the exact generated length.
_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def resolve_device_id(request: Request) -> tuple[str, bool]:
    """Anonymous per-browser identity - no accounts, no login, works on first visit.

    Multi-user, but no auth: each browser gets its own opaque device-id cookie, which is
    the key for that visitor's saved home/radius (see resolve_home). Clearing cookies or
    switching browsers/devices starts a "new" visitor with the default home - an accepted
    tradeoff for zero-friction use over cross-device sync.

    Returns ``(device_id, is_new)`` - callers must set the cookie on their actual response
    object when ``is_new``, via ``set_device_cookie`` below. Every route that needs this
    takes a ``response: Response`` param and returns a plain model/list rather than building
    its own ``JSONResponse`` - FastAPI merges cookies set on the injected ``Response`` onto
    the real response in that case, and it's also required for an accurate response schema
    (FastAPI can't infer one from a route that returns a ``Response`` instance directly).
    """
    device_id = request.cookies.get(_DEVICE_ID_COOKIE)
    if device_id and _DEVICE_ID_PATTERN.fullmatch(device_id):
        return device_id, False
    return secrets.token_urlsafe(32), True


def set_device_cookie(request: Request, response: Response, device_id: str) -> None:
    response.set_cookie(
        _DEVICE_ID_COOKIE,
        device_id,
        max_age=_DEVICE_ID_MAX_AGE,
        httponly=True,
        secure=is_https(request),
        samesite="lax",
    )


def resolve_home(conn: psycopg.Connection, device_id: str, cfg: Settings) -> Home:
    """This visitor's saved home/radius, falling back to the env-configured default."""
    override = db_load_location(conn, device_id)
    return Home(**override) if override is not None else cfg.home


def resolve_genera(conn: psycopg.Connection, device_id: str) -> list[int]:
    """This visitor's selected genera.

    Empty means "everything nearby" (no filter), not the old curated 21 - see
    ``scoring``'s ``_taxon_filter`` for how that's honored in SQL.
    """
    return db_load_genera(conn, device_id)


def require_idle(state: AppState) -> None:
    if state.refreshing:
        raise HTTPException(409, "refreshing data for this area - try again shortly")


_REFRESH_RATE_LIMIT_SECONDS = 300.0


def check_refresh_rate_limit(state: AppState, ip: str) -> None:
    now = time.monotonic()
    limiter = state.refresh_rate_limit
    with state.refresh_rate_limit_lock:
        last = limiter.get(ip)
        if last is not None and now - last < _REFRESH_RATE_LIMIT_SECONDS:
            retry_after = int(_REFRESH_RATE_LIMIT_SECONDS - (now - last)) + 1
            raise HTTPException(
                429,
                f"refresh rate limit: try again in {retry_after}s",
                headers={"Retry-After": str(retry_after)},
            )
        limiter[ip] = now
        for stale_ip in [key for key, ts in limiter.items() if now - ts >= _REFRESH_RATE_LIMIT_SECONDS]:
            del limiter[stale_ip]


def parse_months(months: str) -> list[int]:
    try:
        values = parse_month_list(months)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return values or list(range(1, 13))


def parse_species(species: str, conn: psycopg.Connection, device_id: str) -> list[int]:
    if species == "all" or not species:
        return resolve_genera(conn, device_id)
    try:
        return [int(token) for token in species.split(",") if token.strip()]
    except ValueError as error:
        raise HTTPException(400, f"bad species: {species}") from error


def region_center(region_id: str, cfg: Settings) -> tuple[float, float]:
    """Grid-cell center for a region id ("{ilat}_{ilng}"), inverse of scoring's binning."""
    try:
        ilat_str, ilng_str = region_id.split("_", 1)
        ilat, ilng = int(ilat_str), int(ilng_str)
    except ValueError as error:
        raise HTTPException(400, f"bad region_id: {region_id}") from error
    cell = cfg.cell_deg
    return (ilat + 0.5) * cell, (ilng + 0.5) * cell
