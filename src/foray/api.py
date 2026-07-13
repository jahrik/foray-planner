"""FastAPI app: JSON API over the scoring engine + the server-rendered web UI."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import queue
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from foray import camps, dispersed, geocode, land, scoring, trails
from foray.cache import _ENABLE_POSTGIS, SCHEMA
from foray.cache import load_location as db_load_location
from foray.cache import save_location as db_save_location
from foray.config import Config, Home, load_config
from foray.ingest import ingest

logger = logging.getLogger(__name__)

# The client is a Vite/TypeScript app (see frontend/); `npm run build` emits its bundle
# here. Absent only when the frontend hasn't been built (e.g. a fresh checkout running the
# API directly) - `/` then shows a hint instead of 500-ing so `foray openapi` still works.
_WEB = Path(__file__).parent / "web"
_DIST = _WEB / "dist"


class LocationBody(BaseModel):
    query: str | None = None
    lat: float | None = None
    lng: float | None = None
    name: str | None = None
    radius_km: float | None = None


def create_app(cfg: Config | None = None) -> FastAPI:
    """Wire up the API: a Postgres connection pool + config state, opened/closed via lifespan."""
    cfg = cfg or load_config()

    # Pool connections carry PG* env vars by default (see cache.connect's docstring) - no
    # DSN-building code needed. `open=False` defers the actual connections until the
    # lifespan's `pool.open()`, matching psycopg_pool's recommended startup pattern.
    pool = ConnectionPool(
        conninfo="", min_size=1, max_size=5, open=False, kwargs={"autocommit": True}
    )
    state: dict[str, Any] = {
        "cfg": cfg,
        "refreshing": False,
        "last_error": None,
        "listeners": [],
        "listeners_lock": threading.Lock(),
        "last_progress": None,
        "abort_event": threading.Event(),
        "http_client": None,
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.open()
        with pool.connection() as conn:
            try:
                conn.execute(_ENABLE_POSTGIS)
            except psycopg.errors.InsufficientPrivilege:
                logger.warning(
                    "api: app role lacks CREATE EXTENSION privilege - postgis not enabled; "
                    "the dispersed-camping proxy will be skipped."
                )
            conn.execute(SCHEMA)
            override = db_load_location(conn)
        if override is not None:
            state["cfg"] = state["cfg"].model_copy(update={"home": Home(**override)})
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(title="Foray Planner API", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    if (_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    def broadcast(msg: dict[str, Any]) -> None:
        state["last_progress"] = msg
        with state["listeners_lock"]:
            listener_queues = list(state["listeners"])
        for listener_queue in listener_queues:
            try:
                listener_queue.put_nowait(msg)
            except queue.Full:
                try:
                    listener_queue.get_nowait()
                    listener_queue.put_nowait(msg)
                except (queue.Empty, queue.Full):
                    pass

    def make_cb(base_pct: float, range_pct: float) -> Any:
        def cb(step: str, local_pct: float) -> None:
            broadcast({"step": step, "progress": base_pct + range_pct * (local_pct / 100.0)})

        return cb

    def current() -> Config:
        return state["cfg"]

    def require_idle() -> None:
        if state["refreshing"]:
            raise HTTPException(409, "refreshing data for this area - try again shortly")

    def parse_months(months: str) -> list[int]:
        try:
            values = [int(token) for token in months.split(",") if token.strip()]
        except ValueError as error:
            raise HTTPException(400, f"bad months: {months}") from error
        if not all(1 <= month <= 12 for month in values):
            raise HTTPException(400, "months must be 1-12")
        return values or list(range(1, 13))

    def parse_species(species: str) -> list[int]:
        if species == "all" or not species:
            return current().taxon_ids
        try:
            return [int(token) for token in species.split(",") if token.strip()]
        except ValueError as error:
            raise HTTPException(400, f"bad species: {species}") from error

    def region_center(region_id: str) -> tuple[float, float]:
        """Grid-cell center for a region id ("{ilat}_{ilng}"), inverse of scoring's binning."""
        try:
            ilat_str, ilng_str = region_id.split("_", 1)
            ilat, ilng = int(ilat_str), int(ilng_str)
        except ValueError as error:
            raise HTTPException(400, f"bad region_id: {region_id}") from error
        cell = current().cell_deg
        return (ilat + 0.5) * cell, (ilng + 0.5) * cell

    def run_refresh(target: str = "all") -> None:
        try:
            state["abort_event"].clear()
            # 300s covers Overpass trail queries that can take up to 180s; set a
            # generous ceiling so the shared client doesn't cut off slow phases.
            state["http_client"] = httpx.Client(timeout=300.0)

            broadcast({"step": "Starting refresh…", "progress": 0.0})
            logger.info("refresh: starting for %s (target=%s)", current().home.name, target)

            # One pooled connection checked out for the whole refresh - Postgres handles
            # concurrent readers (other requests borrowing their own connections) natively
            # via MVCC, unlike the DuckDB-era single-writer-file model this replaced.
            with pool.connection() as db:
                if target in ("all", "mushrooms") and not state["abort_event"].is_set():
                    ingest(
                        current(),
                        db,
                        progress_cb=make_cb(0.0, 90.0 if target == "mushrooms" else 50.0),
                        abort_event=state["abort_event"],
                    )
                if target in ("all", "camps") and not state["abort_event"].is_set():
                    camps.ingest_campgrounds(
                        current(),
                        db,
                        client=state["http_client"],
                        progress_cb=make_cb(
                            50.0 if target == "all" else 0.0, 10.0 if target == "all" else 100.0
                        ),
                    )
                if target in ("all", "land") and not state["abort_event"].is_set():
                    land.ingest_public_land(
                        current(),
                        db,
                        client=state["http_client"],
                        progress_cb=make_cb(
                            60.0 if target == "all" else 0.0, 10.0 if target == "all" else 100.0
                        ),
                    )
                if target in ("all", "dispersed") and not state["abort_event"].is_set():
                    dispersed.ingest_dispersed(
                        current(),
                        db,
                        client=state["http_client"],
                        progress_cb=make_cb(
                            70.0 if target == "all" else 0.0, 10.0 if target == "all" else 100.0
                        ),
                    )
                if target in ("all", "trails") and not state["abort_event"].is_set():
                    trails.ingest_trails(
                        current(),
                        db,
                        client=state["http_client"],
                        progress_cb=make_cb(
                            80.0 if target == "all" else 0.0, 10.0 if target == "all" else 100.0
                        ),
                    )

                if target in ("all", "mushrooms") and not state["abort_event"].is_set():
                    broadcast({"step": "Building phenology…", "progress": 90.0})
                    logger.info("refresh: building phenology…")
                    scoring.build_phenology(db, current().cell_deg)

            if state["abort_event"].is_set():
                logger.info("refresh: cancelled by user")
                state["last_error"] = "Cancelled"
                broadcast({"error": "Cancelled", "done": True})
            else:
                state["last_error"] = None
                broadcast({"step": "Done", "progress": 100.0, "done": True})
                logger.info("refresh: complete")
        except (httpx.LocalProtocolError, httpx.ReadError, httpx.PoolTimeout):
            logger.info("refresh: network client closed explicitly (cancelled)")
            state["last_error"] = "Cancelled"
            broadcast({"error": "Cancelled", "done": True})
        except Exception as error:  # surface to the UI rather than dying silently
            logger.exception("refresh: failed")
            state["last_error"] = str(error)
            broadcast({"error": str(error), "done": True})
        finally:
            if state["http_client"] is not None:
                state["http_client"].close()
                state["http_client"] = None
            state["refreshing"] = False

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        cfg = current()
        return {
            "home": cfg.home.model_dump(),
            "cell_deg": cfg.cell_deg,
            "recent_weeks": cfg.recent_weeks,
            "refreshing": state["refreshing"],
            "last_error": state["last_error"],
        }

    @app.get("/api/species")
    def get_species() -> list[dict[str, Any]]:
        return [
            {**species.model_dump(), "inat_url": species.inat_url} for species in current().species
        ]

    @app.get("/api/destinations")
    def destinations(
        months: str | None = Query(None),
        species: str = Query("all"),
        radius_km: float | None = Query(None),
    ) -> JSONResponse:
        require_idle()
        cfg = current()
        # No months given -> default to the current calendar month.
        selected_months = parse_months(months) if months is not None else [dt.date.today().month]
        try:
            with pool.connection() as conn:
                ranked = scoring.rank_destinations(
                    conn,
                    months=selected_months,
                    taxon_ids=parse_species(species),
                    home_lat=cfg.home.lat,
                    home_lng=cfg.home.lng,
                    radius_km=radius_km or cfg.home.radius_km,
                    cell_deg=cfg.cell_deg,
                    recent_weeks=cfg.recent_weeks,
                )
        except psycopg.errors.UndefinedTable:
            raise HTTPException(409, "no data for this area yet - click Fetch data") from None
        return JSONResponse([asdict(region) for region in ranked])

    @app.get("/api/calendar")
    def calendar(region_id: str, species: str = Query("all")) -> dict[int, Any]:
        require_idle()
        try:
            with pool.connection() as conn:
                calendar = scoring.place_calendar(
                    conn, region_id=region_id, taxon_ids=parse_species(species)
                )
        except psycopg.errors.UndefinedTable:
            raise HTTPException(409, "no data for this area yet - click Fetch data") from None
        return calendar

    @app.get("/api/alerts")
    def get_alerts(
        species: str = Query("all"),
        weeks: int | None = Query(None),
        radius_km: float | None = Query(None),
    ) -> list[dict[str, Any]]:
        require_idle()
        cfg = current()
        try:
            with pool.connection() as conn:
                return scoring.alerts(
                    conn,
                    taxon_ids=parse_species(species),
                    home_lat=cfg.home.lat,
                    home_lng=cfg.home.lng,
                    radius_km=radius_km or cfg.home.radius_km,
                    cell_deg=cfg.cell_deg,
                    weeks=weeks or cfg.recent_weeks,
                )
        except psycopg.errors.UndefinedTable:
            return []

    @app.get("/api/camps")
    def get_camps(
        region_id: str | None = Query(None),
        lat: float | None = Query(None),
        lng: float | None = Query(None),
        radius_km: float = Query(40.0),
        free_only: bool = Query(False),
    ) -> JSONResponse:
        """Campsites near a region (by id) or an explicit lat/lng, free-first by distance."""
        require_idle()
        if region_id is not None:
            center_lat, center_lng = region_center(region_id)
        elif lat is not None and lng is not None:
            center_lat, center_lng = lat, lng
        else:
            raise HTTPException(400, "provide `region_id` or both `lat` and `lng`")
        with pool.connection() as conn:
            sites = scoring.camps_near(
                conn,
                lat=center_lat,
                lng=center_lng,
                radius_km=radius_km,
                free_only=free_only,
            )
        return JSONResponse([asdict(site) for site in sites])

    @app.get("/api/land")
    def get_land(
        region_id: str | None = Query(None),
        lat: float | None = Query(None),
        lng: float | None = Query(None),
        radius_km: float = Query(40.0),
    ) -> JSONResponse:
        """Public-land ownership polygons near a region (by id) or an explicit lat/lng."""
        require_idle()
        if region_id is not None:
            center_lat, center_lng = region_center(region_id)
        elif lat is not None and lng is not None:
            center_lat, center_lng = lat, lng
        else:
            raise HTTPException(400, "provide `region_id` or both `lat` and `lng`")
        with pool.connection() as conn:
            units = scoring.land_near(conn, lat=center_lat, lng=center_lng, radius_km=radius_km)
        return JSONResponse([asdict(unit) for unit in units])

    @app.get("/api/trails")
    def get_trails(
        region_id: str | None = Query(None),
        lat: float | None = Query(None),
        lng: float | None = Query(None),
        radius_km: float = Query(40.0),
    ) -> JSONResponse:
        """Trails near a region (by id) or an explicit lat/lng, nearest to the hotspot first."""
        require_idle()
        if region_id is not None:
            center_lat, center_lng = region_center(region_id)
        elif lat is not None and lng is not None:
            center_lat, center_lng = lat, lng
        else:
            raise HTTPException(400, "provide `region_id` or both `lat` and `lng`")
        with pool.connection() as conn:
            found = scoring.trails_near(conn, lat=center_lat, lng=center_lng, radius_km=radius_km)
        return JSONResponse([asdict(trail) for trail in found])

    @app.get("/api/plan")
    def plan(
        months: str | None = Query(None),
        species: str = Query("all"),
        radius_km: float | None = Query(None),
        max_stops: int = Query(5, ge=1, le=20),
        max_drive_km: float = Query(400.0, gt=0),
        camp_radius_km: float = Query(40.0, gt=0),
        require_free_camp: bool = Query(True),
    ) -> JSONResponse:
        """Greedy multi-stop itinerary: top destinations sequenced home-out with the least drive."""
        require_idle()
        cfg = current()
        selected_months = parse_months(months) if months is not None else [dt.date.today().month]
        try:
            with pool.connection() as conn:
                trip = scoring.plan_route(
                    conn,
                    months=selected_months,
                    taxon_ids=parse_species(species),
                    home_lat=cfg.home.lat,
                    home_lng=cfg.home.lng,
                    radius_km=radius_km or cfg.home.radius_km,
                    cell_deg=cfg.cell_deg,
                    recent_weeks=cfg.recent_weeks,
                    max_stops=max_stops,
                    max_drive_km=max_drive_km,
                    camp_radius_km=camp_radius_km,
                    require_free_camp=require_free_camp,
                )
        except psycopg.errors.UndefinedTable:
            raise HTTPException(409, "no data for this area yet - click Fetch data") from None
        return JSONResponse(asdict(trip))

    @app.post("/api/location")
    def set_location(body: LocationBody) -> dict[str, Any]:
        cfg = current()
        if body.lat is not None and body.lng is not None:
            home = Home(
                name=body.name or f"{body.lat:.4f}, {body.lng:.4f}",
                lat=body.lat,
                lng=body.lng,
                radius_km=body.radius_km or cfg.home.radius_km,
            )
        elif body.query:
            try:
                location = geocode.resolve(body.query)
            except (LookupError, ValueError) as error:
                raise HTTPException(404, str(error)) from None
            except Exception as error:  # network/geocoder failure
                raise HTTPException(502, f"geocoding failed: {error}") from None
            home = Home(
                name=body.name or location.name,
                lat=location.lat,
                lng=location.lng,
                radius_km=body.radius_km or cfg.home.radius_km,
            )
        else:
            raise HTTPException(400, "provide `query` or both `lat` and `lng`")

        needs_refresh = True
        with pool.connection() as conn:
            db_save_location(
                conn, name=home.name, lat=home.lat, lng=home.lng, radius_km=home.radius_km
            )
            state["cfg"] = cfg.model_copy(update={"home": home})

            try:
                # The core data is mushrooms; check if we've ingested observations for this area.
                key_pattern = f"obs:%:{home.lat}:{home.lng}:{home.radius_km}:%"
                row = conn.execute(
                    "SELECT 1 FROM ingest_log WHERE key LIKE %s", [key_pattern]
                ).fetchone()
                has_obs = row is not None
                phenology_row = conn.execute("SELECT 1 FROM phenology LIMIT 1").fetchone()
                has_phenology = phenology_row is not None
                needs_refresh = not (has_obs and has_phenology)
            except psycopg.errors.UndefinedTable:
                needs_refresh = True

        return {"home": home.model_dump(), "needs_refresh": needs_refresh}

    _VALID_REFRESH_TARGETS = frozenset({"all", "mushrooms", "camps", "land", "dispersed", "trails"})

    @app.post("/api/refresh")
    def refresh(target: str = Query("mushrooms")) -> dict[str, Any]:
        if target not in _VALID_REFRESH_TARGETS:
            raise HTTPException(
                400, f"unknown target '{target}'; valid: {sorted(_VALID_REFRESH_TARGETS)}"
            )
        if state["refreshing"]:
            return {"status": "already running"}
        state["refreshing"] = True
        state["last_error"] = None
        state["last_progress"] = None
        threading.Thread(target=run_refresh, args=(target,), daemon=True).start()
        return {"status": "started"}

    @app.delete("/api/refresh")
    def cancel_refresh() -> dict[str, Any]:
        if state["refreshing"]:
            state["abort_event"].set()
            if state["http_client"] is not None:
                state["http_client"].close()
            return {"status": "cancelling"}
        return {"status": "idle"}

    @app.get("/api/refresh/stream")
    async def refresh_stream() -> StreamingResponse:
        listener_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100)
        if state["last_progress"]:
            listener_queue.put_nowait(state["last_progress"])
        with state["listeners_lock"]:
            state["listeners"].append(listener_queue)

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
                with state["listeners_lock"]:
                    if listener_queue in state["listeners"]:
                        state["listeners"].remove(listener_queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> Any:
        # The SPA fetches /api/config on load, so no server-side templating is needed -
        # just hand back the built entry point.
        if (_DIST / "index.html").is_file():
            return FileResponse(_DIST / "index.html")
        return HTMLResponse(
            "<h1>Foray Planner</h1><p>Frontend not built. Run "
            "<code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</code>.</p>",
            status_code=503,
        )

    return app
