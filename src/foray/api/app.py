"""The FastAPI app factory: a Postgres pool + config state, routers, and middleware.

The route handlers live in :mod:`foray.api.routes` (one ``APIRouter`` per domain) and reach
shared state through ``request.app.state`` via the dependencies in :mod:`foray.api.deps`.
This module only wires them together - see issue #242 for the decomposition of what used to
be a single 850-line ``create_app`` closure.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool

from foray.api.paths import DIST
from foray.api.routes import (
    config,
    coverage,
    destinations,
    genera,
    index,
    layers,
    location,
    plan,
    refresh,
)
from foray.api.security import install_middleware
from foray.api.state import AppState
from foray.cache import apply_schema
from foray.config import Settings
from foray.logging_config import setup_logging

logger = logging.getLogger(__name__)

# Registered in the order their paths should appear in the OpenAPI schema (drift-checked).
_ROUTERS = (
    config.router,
    genera.router,
    coverage.router,
    destinations.router,
    layers.router,
    plan.router,
    location.router,
    refresh.router,
    index.router,
)


def create_app(cfg: Settings | None = None) -> FastAPI:
    """Wire up the API: a Postgres connection pool + config state, opened/closed via lifespan."""
    setup_logging()
    cfg = cfg or Settings()

    # Pool connections carry PG* env vars by default (see cache.connect's docstring) - no
    # DSN-building code needed. `open=False` defers the actual connections until the
    # lifespan's `pool.open()`, matching psycopg_pool's recommended startup pattern.
    pool = ConnectionPool(conninfo="", min_size=1, max_size=5, open=False, kwargs={"autocommit": True})
    state = AppState(cfg=cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.open()
        with pool.connection() as conn:
            # Full schema + migration chain, not just the CREATE TABLE baseline - the server
            # never calls cache.connect(), so this is the only place migrations get applied
            # in-process (a stale prod column otherwise waits on an out-of-process cron run).
            apply_schema(conn)
        # `state.cfg.home` is now only ever the env/default home - see resolve_device_id
        # and resolve_home for per-visitor overrides. Multi-user, no accounts: each browser
        # gets its own anonymous device-id cookie and its own saved home/radius in `app_location`.
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(title="Foray Planner API", lifespan=lifespan)
    app.state.foray = state
    app.state.pool = pool

    app.add_middleware(GZipMiddleware, minimum_size=1000)
    install_middleware(app)

    if (DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")

    for router in _ROUTERS:
        app.include_router(router)

    # vite-plugin-pwa emits the manifest, service worker, and icons as root-level files in
    # dist/ (not under assets/); the "/" route above already claims the exact root path, so
    # this mount only ever serves the other root-level files.
    if DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(DIST)), name="pwa-assets")

    return app
