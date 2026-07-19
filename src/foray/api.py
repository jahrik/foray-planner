"""FastAPI app: JSON API over the scoring engine + the server-rendered web UI."""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import json
import logging
import queue
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from foray import camps, dispersed, geocode, inat, land, scoring, trails
from foray.api_models import (
    AlertRegion,
    CalendarBucket,
    CampSite,
    ConfigResponse,
    CoverageRegionResponse,
    GenusResult,
    LandUnit,
    LocationResponse,
    RecentObservation,
    RegionScore,
    StatusResponse,
    Trail,
    TripPlan,
)
from foray.cache import _ENABLE_POSTGIS, SCHEMA, search_fungi_genera
from foray.cache import add_genus as db_add_genus
from foray.cache import list_selected_genera as db_list_selected_genera
from foray.cache import load_genera as db_load_genera
from foray.cache import load_location as db_load_location
from foray.cache import remove_genus as db_remove_genus
from foray.cache import save_location as db_save_location
from foray.config import Config, Home, Settings
from foray.ingest import ingest

logger = logging.getLogger(__name__)

# The client is a Vite/TypeScript app (see frontend/); `npm run build` emits its bundle
# here. Absent only when the frontend hasn't been built (e.g. a fresh checkout running the
# API directly) - `/` then shows a hint instead of 500-ing so `foray openapi` still works.
_WEB = Path(__file__).parent / "web"
_DIST = _WEB / "dist"


# Public-facing app serving an HTML+JS frontend - locked down to what the frontend actually
# needs (Leaflet bundled as 'self', OSM tiles/Nominatim as the only third-party origins) so an
# XSS bug can't exfiltrate to or load script from anywhere else. style-src needs 'unsafe-inline'
# because the frontend sets `style="..."` attributes directly (map legend swatches, score bars,
# phenology heatmap cells) - much lower risk than script injection, so that's an accepted gap.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' https://*.tile.openstreetmap.org "
    "https://static.inaturalist.org https://inaturalist-open-data.s3.amazonaws.com data:; "
    "connect-src 'self' https://nominatim.openstreetmap.org; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def _is_https(request: Request) -> bool:
    # Cloudflare terminates TLS and proxies to the droplet over plain HTTP, setting
    # X-Forwarded-Proto to the client-facing scheme - trust that over the raw connection
    # scheme so this is accurate in prod. Falls back to the direct scheme for local dev
    # (no proxy in front), so behavior stays correct over plain http://localhost too.
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _client_ip(request: Request) -> str:
    # The origin firewall only accepts inbound 80/443 from Cloudflare's ranges, so
    # CF-Connecting-IP is safe to trust - but only after confirming it's actually an IP,
    # since a misconfigured proxy or local dev could hand us arbitrary header junk that
    # would otherwise let the rate-limit dict grow unbounded and bypass per-IP limiting.
    header = request.headers.get("cf-connecting-ip")
    if header:
        try:
            ipaddress.ip_address(header)
            return header
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


class LocationBody(BaseModel):
    query: str | None = Field(default=None, max_length=200)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    name: str | None = Field(default=None, max_length=200)
    radius_km: float | None = Field(default=None, gt=0, le=500)


def create_app(cfg: Config | None = None) -> FastAPI:
    """Wire up the API: a Postgres connection pool + config state, opened/closed via lifespan."""
    cfg = cfg or Settings()

    # Pool connections carry PG* env vars by default (see cache.connect's docstring) - no
    # DSN-building code needed. `open=False` defers the actual connections until the
    # lifespan's `pool.open()`, matching psycopg_pool's recommended startup pattern.
    pool = ConnectionPool(conninfo="", min_size=1, max_size=5, open=False, kwargs={"autocommit": True})
    state: dict[str, Any] = {
        "cfg": cfg,
        "refreshing": False,
        "last_error": None,
        "listeners": [],
        "listeners_lock": threading.Lock(),
        "last_progress": None,
        "abort_event": threading.Event(),
        "http_client": None,
        "refresh_rate_limit": {},
        "refresh_rate_limit_lock": threading.Lock(),
        "refresh_lock": threading.Lock(),
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.open()
        with pool.connection() as conn:
            try:
                conn.execute(_ENABLE_POSTGIS)
            except psycopg.Error:
                logger.warning(
                    "api: could not enable postgis (missing extension or insufficient privilege); "
                    "the dispersed-camping proxy will be skipped."
                )
            conn.execute(SCHEMA)
        # `state["cfg"].home` is now only ever the env/default home - see resolve_device_id
        # and resolve_home below for per-visitor overrides. Multi-user, no accounts: each browser
        # gets its own anonymous device-id cookie and its own saved home/radius in `app_location`.
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(title="Foray Planner API", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
        if _is_https(request):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

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
            secure=_is_https(request),
            samesite="lax",
        )

    def resolve_home(conn: psycopg.Connection, device_id: str) -> Home:
        """This visitor's saved home/radius, falling back to the env-configured default."""
        override = db_load_location(conn, device_id)
        return Home(**override) if override is not None else current().home

    def resolve_genera(conn: psycopg.Connection, device_id: str) -> list[int]:
        """This visitor's selected genera.

        Empty means "everything nearby" (no filter), not the old curated 21 - see
        ``scoring.py``'s ``_taxon_filter`` for how that's honored in SQL.
        """
        return db_load_genera(conn, device_id)

    def require_idle() -> None:
        if state["refreshing"]:
            raise HTTPException(409, "refreshing data for this area - try again shortly")

    _REFRESH_RATE_LIMIT_SECONDS = 300.0

    def check_refresh_rate_limit(ip: str) -> None:
        now = time.monotonic()
        limiter: dict[str, float] = state["refresh_rate_limit"]
        with state["refresh_rate_limit_lock"]:
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
            values = [int(token) for token in months.split(",") if token.strip()]
        except ValueError as error:
            raise HTTPException(400, f"bad months: {months}") from error
        if not all(1 <= month <= 12 for month in values):
            raise HTTPException(400, "months must be 1-12")
        return values or list(range(1, 13))

    def parse_species(species: str, conn: psycopg.Connection, device_id: str) -> list[int]:
        if species == "all" or not species:
            return resolve_genera(conn, device_id)
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

    def run_refresh(home: Home, target: str = "all") -> None:
        # Refresh ingests around *this visitor's* home, not the env-configured default - a
        # per-request Config with `.home` swapped in lets ingest()/camps.py/land.py/etc. stay
        # unchanged (they all just read `cfg.home` internally).
        refresh_cfg = current().model_copy(update={"home": home})
        try:
            state["abort_event"].clear()
            # 300s covers Overpass trail queries that can take up to 180s; set a
            # generous ceiling so the shared client doesn't cut off slow phases.
            state["http_client"] = httpx.Client(timeout=300.0)

            broadcast({"step": "Starting refresh…", "progress": 0.0})
            logger.info("refresh: starting for %s (target=%s)", refresh_cfg.home.name, target)

            # One pooled connection checked out for the whole refresh - Postgres handles
            # concurrent readers (other requests borrowing their own connections) natively
            # via MVCC, unlike the DuckDB-era single-writer-file model this replaced.
            with pool.connection() as db:
                if target in ("all", "mushrooms") and not state["abort_event"].is_set():
                    ingest(
                        refresh_cfg,
                        db,
                        progress_cb=make_cb(0.0, 90.0 if target == "mushrooms" else 50.0),
                        abort_event=state["abort_event"],
                    )
                if target in ("all", "camps") and not state["abort_event"].is_set():
                    camps.ingest_campgrounds(
                        refresh_cfg,
                        db,
                        client=state["http_client"],
                        progress_cb=make_cb(50.0 if target == "all" else 0.0, 10.0 if target == "all" else 100.0),
                    )
                if target in ("all", "land") and not state["abort_event"].is_set():
                    land.ingest_public_land(
                        refresh_cfg,
                        db,
                        client=state["http_client"],
                        progress_cb=make_cb(60.0 if target == "all" else 0.0, 10.0 if target == "all" else 100.0),
                    )
                if target in ("all", "dispersed") and not state["abort_event"].is_set():
                    dispersed.ingest_dispersed(
                        refresh_cfg,
                        db,
                        client=state["http_client"],
                        progress_cb=make_cb(70.0 if target == "all" else 0.0, 10.0 if target == "all" else 100.0),
                    )
                if target in ("all", "trails") and not state["abort_event"].is_set():
                    trails.ingest_trails(
                        refresh_cfg,
                        db,
                        client=state["http_client"],
                        progress_cb=make_cb(80.0 if target == "all" else 0.0, 10.0 if target == "all" else 100.0),
                    )

                if target in ("all", "mushrooms") and not state["abort_event"].is_set():
                    broadcast({"step": "Building phenology…", "progress": 90.0})
                    logger.info("refresh: building phenology…")
                    scoring.build_phenology(db, refresh_cfg.cell_deg)

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
    def get_config(request: Request, response: Response) -> ConfigResponse:
        cfg = current()
        device_id, is_new = resolve_device_id(request)
        if is_new:
            set_device_cookie(request, response, device_id)
        with pool.connection() as conn:
            home = resolve_home(conn, device_id)
        return ConfigResponse(
            home=home,
            cell_deg=cfg.cell_deg,
            recent_weeks=cfg.recent_weeks,
            refreshing=state["refreshing"],
            last_error=state["last_error"],
        )

    @app.get("/api/genera")
    def get_genera(query: str = Query("", alias="q", max_length=200)) -> list[GenusResult]:
        """Genus catalog search (issue #79) - empty query returns the most-observed genera."""
        with pool.connection() as conn:
            hits = search_fungi_genera(conn, query)
        return [GenusResult(**hit) for hit in hits]

    @app.get("/api/genera/selected")
    def get_selected_genera(request: Request, response: Response) -> list[GenusResult]:
        """This device's selected genera (issue #79 Phase 2) - empty means "everything nearby"."""
        device_id, is_new = resolve_device_id(request)
        if is_new:
            set_device_cookie(request, response, device_id)
        with pool.connection() as conn:
            hits = db_list_selected_genera(conn, device_id)
        return [GenusResult(**hit) for hit in hits]

    @app.post("/api/genera/{taxon_id}")
    def add_selected_genus(taxon_id: int, request: Request, response: Response) -> StatusResponse:
        device_id, is_new = resolve_device_id(request)
        if is_new:
            set_device_cookie(request, response, device_id)
        with pool.connection() as conn:
            db_add_genus(conn, device_id, taxon_id)
        return StatusResponse(status="added")

    @app.delete("/api/genera/{taxon_id}")
    def remove_selected_genus(taxon_id: int, request: Request, response: Response) -> StatusResponse:
        device_id, is_new = resolve_device_id(request)
        if is_new:
            set_device_cookie(request, response, device_id)
        with pool.connection() as conn:
            db_remove_genus(conn, device_id, taxon_id)
        return StatusResponse(status="removed")

    @app.get("/api/coverage")
    def get_coverage() -> list[CoverageRegionResponse]:
        """Coverage regions with their latest ingest timestamps."""
        cfg = current()
        with pool.connection() as conn:
            results = []
            for region in cfg.coverage:
                row = conn.execute(
                    "SELECT max(fetched_at) FROM ingest_log WHERE key LIKE %s",
                    [f"obs:%:place:{region.place_id}:%"],
                ).fetchone()
                last_ingest = row[0].isoformat() if row and row[0] else None
                # Since issue #79 Phase 4, ingest_region() writes one whole-Fungi-kingdom
                # ingest_log row per (place, window) instead of one per taxon - "observations
                # ingested" (row_count) is the meaningful count now, not "distinct taxa".
                count_row = conn.execute(
                    "SELECT COALESCE(sum(row_count), 0) FROM ingest_log WHERE key LIKE %s",
                    [f"obs:%:place:{region.place_id}:%"],
                ).fetchone()
                results.append(
                    CoverageRegionResponse(
                        name=region.name,
                        place_id=region.place_id,
                        last_ingest=last_ingest,
                        observations_ingested=count_row[0] if count_row else 0,
                    )
                )
        return results

    @app.get("/api/destinations")
    def destinations(
        request: Request,
        response: Response,
        months: str | None = Query(None),
        species: str = Query("all"),
        radius_km: float | None = Query(None),
    ) -> list[RegionScore]:
        require_idle()
        cfg = current()
        device_id, is_new = resolve_device_id(request)
        if is_new:
            set_device_cookie(request, response, device_id)
        # No months given -> default to the current calendar month.
        selected_months = parse_months(months) if months is not None else [dt.date.today().month]
        try:
            with pool.connection() as conn:
                home = resolve_home(conn, device_id)
                ranked = scoring.rank_destinations(
                    conn,
                    months=selected_months,
                    taxon_ids=parse_species(species, conn, device_id),
                    home_lat=home.lat,
                    home_lng=home.lng,
                    radius_km=radius_km or home.radius_km,
                    cell_deg=cfg.cell_deg,
                    recent_weeks=cfg.recent_weeks,
                )
        except psycopg.errors.UndefinedTable:
            raise HTTPException(409, "no data for this area yet - click Fetch data") from None
        return [RegionScore.model_validate(region) for region in ranked]

    @app.get("/api/calendar")
    def calendar(
        region_id: str, request: Request, response: Response, species: str = Query("all")
    ) -> dict[str, CalendarBucket]:
        require_idle()
        device_id, is_new = resolve_device_id(request)
        if is_new:
            set_device_cookie(request, response, device_id)
        try:
            with pool.connection() as conn:
                calendar = scoring.place_calendar(
                    conn, region_id=region_id, taxon_ids=parse_species(species, conn, device_id)
                )
        except psycopg.errors.UndefinedTable:
            raise HTTPException(409, "no data for this area yet - click Fetch data") from None
        return {str(month): CalendarBucket.model_validate(bucket) for month, bucket in calendar.items()}

    @app.get("/api/observations/photos")
    def observation_photos(
        region_id: str, request: Request, response: Response, species: str = Query("all")
    ) -> list[RecentObservation]:
        require_idle()
        cfg = current()
        device_id, is_new = resolve_device_id(request)
        if is_new:
            set_device_cookie(request, response, device_id)
        try:
            with pool.connection() as conn:
                recent = scoring.recent_observations(
                    conn,
                    region_id=region_id,
                    taxon_ids=parse_species(species, conn, device_id),
                    cell_deg=cfg.cell_deg,
                )
        except psycopg.errors.UndefinedTable:
            raise HTTPException(409, "no data for this area yet - click Fetch data") from None
        photos_by_obs = inat.photos_for_observations([obs["id"] for obs in recent])
        result = []
        for obs in recent:
            photos = [
                {"url": photo["url"], "license_code": photo["license_code"], "attribution": photo["attribution"]}
                for photo in photos_by_obs.get(obs["id"], [])
                if photo.get("license_code") in inat.DISPLAYABLE_PHOTO_LICENSES
            ]
            result.append(RecentObservation.model_validate({**obs, "photos": photos}))
        return result

    @app.get("/api/alerts")
    def get_alerts(
        request: Request,
        response: Response,
        species: str = Query("all"),
        weeks: int | None = Query(None),
        radius_km: float | None = Query(None),
    ) -> list[AlertRegion]:
        require_idle()
        cfg = current()
        device_id, is_new = resolve_device_id(request)
        if is_new:
            set_device_cookie(request, response, device_id)
        try:
            with pool.connection() as conn:
                home = resolve_home(conn, device_id)
                regions = scoring.alerts(
                    conn,
                    taxon_ids=parse_species(species, conn, device_id),
                    home_lat=home.lat,
                    home_lng=home.lng,
                    radius_km=radius_km or home.radius_km,
                    cell_deg=cfg.cell_deg,
                    weeks=weeks or cfg.recent_weeks,
                )
        except psycopg.errors.UndefinedTable:
            return []
        return [AlertRegion.model_validate(region) for region in regions]

    @app.get("/api/camps")
    def get_camps(
        region_id: str | None = Query(None),
        lat: float | None = Query(None),
        lng: float | None = Query(None),
        radius_km: float = Query(40.0),
        free_only: bool = Query(False),
    ) -> list[CampSite]:
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
        return [CampSite.model_validate(site) for site in sites]

    @app.get("/api/land")
    def get_land(
        region_id: str | None = Query(None),
        lat: float | None = Query(None),
        lng: float | None = Query(None),
        radius_km: float = Query(40.0),
    ) -> list[LandUnit]:
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
        return [LandUnit.model_validate(unit) for unit in units]

    @app.get("/api/trails")
    def get_trails(
        region_id: str | None = Query(None),
        lat: float | None = Query(None),
        lng: float | None = Query(None),
        radius_km: float = Query(40.0),
    ) -> list[Trail]:
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
        return [Trail.model_validate(trail) for trail in found]

    @app.get("/api/plan")
    def plan(
        request: Request,
        response: Response,
        months: str | None = Query(None),
        species: str = Query("all"),
        radius_km: float | None = Query(None),
        max_stops: int = Query(5, ge=1, le=20),
        max_drive_km: float = Query(400.0, gt=0),
        camp_radius_km: float = Query(40.0, gt=0),
        require_free_camp: bool = Query(True),
    ) -> TripPlan:
        """Greedy multi-stop itinerary: top destinations sequenced home-out with the least drive."""
        require_idle()
        cfg = current()
        device_id, is_new = resolve_device_id(request)
        if is_new:
            set_device_cookie(request, response, device_id)
        selected_months = parse_months(months) if months is not None else [dt.date.today().month]
        try:
            with pool.connection() as conn:
                home = resolve_home(conn, device_id)
                trip = scoring.plan_route(
                    conn,
                    months=selected_months,
                    taxon_ids=parse_species(species, conn, device_id),
                    home_lat=home.lat,
                    home_lng=home.lng,
                    radius_km=radius_km or home.radius_km,
                    cell_deg=cfg.cell_deg,
                    recent_weeks=cfg.recent_weeks,
                    max_stops=max_stops,
                    max_drive_km=max_drive_km,
                    camp_radius_km=camp_radius_km,
                    require_free_camp=require_free_camp,
                )
        except psycopg.errors.UndefinedTable:
            raise HTTPException(409, "no data for this area yet - click Fetch data") from None
        return TripPlan.model_validate(trip)

    @app.post("/api/location")
    def set_location(body: LocationBody, request: Request, response: Response) -> LocationResponse:
        device_id, is_new = resolve_device_id(request)
        if is_new:
            set_device_cookie(request, response, device_id)
        with pool.connection() as conn:
            current_home = resolve_home(conn, device_id)
            if body.lat is not None and body.lng is not None:
                home = Home(
                    name=body.name or f"{body.lat:.4f}, {body.lng:.4f}",
                    lat=body.lat,
                    lng=body.lng,
                    radius_km=body.radius_km or current_home.radius_km,
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
                    radius_km=body.radius_km or current_home.radius_km,
                )
            else:
                raise HTTPException(400, "provide `query` or both `lat` and `lng`")

            db_save_location(
                conn, device_id=device_id, name=home.name, lat=home.lat, lng=home.lng, radius_km=home.radius_km
            )

        return LocationResponse(home=home)

    _VALID_REFRESH_TARGETS = frozenset({"all", "mushrooms", "camps", "land", "dispersed", "trails"})

    @app.post("/api/refresh")
    def refresh(request: Request, response: Response, target: str = Query("mushrooms")) -> StatusResponse:
        if target not in _VALID_REFRESH_TARGETS:
            raise HTTPException(400, f"unknown target '{target}'; valid: {sorted(_VALID_REFRESH_TARGETS)}")
        # Check-and-set must be one atomic step, else two concurrent requests can both see
        # `refreshing=False` and both start a refresh thread.
        with state["refresh_lock"]:
            if state["refreshing"]:
                return StatusResponse(status="already running")
            state["refreshing"] = True
        try:
            check_refresh_rate_limit(_client_ip(request))
            device_id, is_new = resolve_device_id(request)
            if is_new:
                set_device_cookie(request, response, device_id)
            with pool.connection() as conn:
                home = resolve_home(conn, device_id)
        except Exception:
            state["refreshing"] = False
            raise
        state["last_error"] = None
        state["last_progress"] = None
        threading.Thread(target=run_refresh, args=(home, target), daemon=True).start()
        return StatusResponse(status="started")

    @app.delete("/api/refresh")
    def cancel_refresh() -> StatusResponse:
        if state["refreshing"]:
            state["abort_event"].set()
            if state["http_client"] is not None:
                state["http_client"].close()
            return StatusResponse(status="cancelling")
        return StatusResponse(status="idle")

    @app.get(
        "/api/refresh/stream",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Server-sent progress events",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            }
        },
    )
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

    # vite-plugin-pwa emits the manifest, service worker, and icons as root-level files in
    # dist/ (not under assets/); the "/" route above already claims the exact root path, so
    # this mount only ever serves the other root-level files.
    if _DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(_DIST)), name="pwa-assets")

    return app
