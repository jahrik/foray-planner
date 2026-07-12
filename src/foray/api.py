"""FastAPI app: JSON API over the scoring engine + the server-rendered web UI."""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from foray import camps, dispersed, geocode, land, scoring, trails
from foray.cache import connect
from foray.config import Config, Home, load_config, location_path, save_location
from foray.ingest import ingest

logger = logging.getLogger(__name__)

# The client is a Vite/TypeScript app (see frontend/); `npm run build` emits its bundle
# here. Absent only when the frontend hasn't been built (e.g. a fresh checkout running the
# API directly) — `/` then shows a hint instead of 500-ing so `foray openapi` still works.
_WEB = Path(__file__).parent / "web"
_DIST = _WEB / "dist"


class LocationBody(BaseModel):
    query: str | None = None
    lat: float | None = None
    lng: float | None = None
    name: str | None = None
    radius_km: float | None = None


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="Foray Planner")
    if (_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    # One shared read-write connection for the whole app. Per-request cursors are
    # thread-safe, and a background refresh writing through the same connection avoids
    # the file-lock conflict that separate connections would hit.
    db = connect(cfg.db_path)
    state: dict[str, Any] = {"cfg": cfg, "refreshing": False, "last_error": None}

    def current() -> Config:
        return state["cfg"]

    def require_idle() -> None:
        if state["refreshing"]:
            raise HTTPException(409, "refreshing data for this area — try again shortly")

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

    def run_refresh() -> None:
        try:
            logger.info("refresh: starting for %s", current().home.name)
            ingest(current(), db)
            camps.ingest_campgrounds(current(), db)
            land.ingest_public_land(current(), db)
            dispersed.ingest_dispersed(current(), db)  # after land: proxy intersects public_land
            trails.ingest_trails(current(), db)
            logger.info("refresh: building phenology…")
            scoring.build_phenology(db, current().cell_deg)
            state["last_error"] = None
            logger.info("refresh: complete")
        except Exception as error:  # surface to the UI rather than dying silently
            logger.exception("refresh: failed")
            state["last_error"] = str(error)
        finally:
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
        cursor = db.cursor()
        try:
            ranked = scoring.rank_destinations(
                cursor,
                months=selected_months,
                taxon_ids=parse_species(species),
                home_lat=cfg.home.lat,
                home_lng=cfg.home.lng,
                radius_km=radius_km or cfg.home.radius_km,
                cell_deg=cfg.cell_deg,
                recent_weeks=cfg.recent_weeks,
            )
        except duckdb.CatalogException:
            raise HTTPException(409, "no data for this area yet — click Fetch data") from None
        finally:
            cursor.close()
        return JSONResponse([asdict(region) for region in ranked])

    @app.get("/api/calendar")
    def calendar(region_id: str, species: str = Query("all")) -> dict[int, Any]:
        require_idle()
        cursor = db.cursor()
        try:
            calendar = scoring.place_calendar(
                cursor, region_id=region_id, taxon_ids=parse_species(species)
            )
        except duckdb.CatalogException:
            raise HTTPException(409, "no data for this area yet — click Fetch data") from None
        finally:
            cursor.close()
        return calendar

    @app.get("/api/alerts")
    def get_alerts(
        species: str = Query("all"),
        weeks: int | None = Query(None),
        radius_km: float | None = Query(None),
    ) -> list[dict[str, Any]]:
        require_idle()
        cfg = current()
        cursor = db.cursor()
        try:
            return scoring.alerts(
                cursor,
                taxon_ids=parse_species(species),
                home_lat=cfg.home.lat,
                home_lng=cfg.home.lng,
                radius_km=radius_km or cfg.home.radius_km,
                cell_deg=cfg.cell_deg,
                weeks=weeks or cfg.recent_weeks,
            )
        except duckdb.CatalogException:
            return []
        finally:
            cursor.close()

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
        cursor = db.cursor()
        try:
            sites = scoring.camps_near(
                cursor,
                lat=center_lat,
                lng=center_lng,
                radius_km=radius_km,
                free_only=free_only,
            )
        finally:
            cursor.close()
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
        cursor = db.cursor()
        try:
            units = scoring.land_near(cursor, lat=center_lat, lng=center_lng, radius_km=radius_km)
        finally:
            cursor.close()
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
        cursor = db.cursor()
        try:
            found = scoring.trails_near(cursor, lat=center_lat, lng=center_lng, radius_km=radius_km)
        finally:
            cursor.close()
        return JSONResponse([asdict(trail) for trail in found])

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

        save_location(location_path(cfg.db_path), home)
        state["cfg"] = cfg.model_copy(update={"home": home})
        return {"home": home.model_dump()}

    @app.post("/api/refresh")
    def refresh() -> dict[str, Any]:
        if state["refreshing"]:
            return {"status": "already running"}
        state["refreshing"] = True
        state["last_error"] = None
        threading.Thread(target=run_refresh, daemon=True).start()
        return {"status": "started"}

    @app.get("/", response_class=HTMLResponse)
    def index() -> Any:
        # The SPA fetches /api/config on load, so no server-side templating is needed —
        # just hand back the built entry point.
        if (_DIST / "index.html").is_file():
            return FileResponse(_DIST / "index.html")
        return HTMLResponse(
            "<h1>Foray Planner</h1><p>Frontend not built. Run "
            "<code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</code>.</p>",
            status_code=503,
        )

    return app
