"""Configuration via pydantic-settings.

All settings come from environment variables (prefix ``FORAY_``, nested delimiter ``__``)
or a ``.env`` file. Complex types (species list, coverage regions) are JSON-encoded env vars.
Defaults for species and coverage are built into the app via ``foray.defaults``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from foray.defaults import CELL_DEG as _DEFAULT_CELL_DEG
from foray.defaults import COUNTRIES as _DEFAULT_COUNTRIES
from foray.defaults import COVERAGE as _DEFAULT_COVERAGE
from foray.defaults import HOME_LAT as _DEFAULT_HOME_LAT
from foray.defaults import HOME_LNG as _DEFAULT_HOME_LNG
from foray.defaults import HOME_RADIUS_KM as _DEFAULT_HOME_RADIUS_KM

QualityGrade = Literal["research", "needs_id", "casual"]


class Home(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = "Home"
    lat: float = Field(ge=-90, le=90, default=_DEFAULT_HOME_LAT)
    lng: float = Field(ge=-180, le=180, default=_DEFAULT_HOME_LNG)
    radius_km: float = Field(gt=0, le=20000, default=_DEFAULT_HOME_RADIUS_KM)


class Ingest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    since_year: int = Field(ge=1900, le=2100, default=2015)
    quality_grade: QualityGrade = "research"
    recent_weeks: int = Field(gt=0, le=520, default=4)
    # Cap on how far back a coverage-region ingest (the nightly --countries cron job) will ever
    # look, regardless of whether it's a taxon's first run for that region. Full historical
    # coverage comes from a one-time bulk load (see data/ scratch scripts), not this path - a
    # first-run country ingest used to fall back to since_year (2015), which repeatedly crashed
    # the droplet with ENOSPC on genera whose backfill never finished.
    region_sync_days: int = Field(gt=0, le=3650, default=30)


class Intervals(BaseModel):
    """Expected cadence (hours) for each scheduled job, so ``/healthz/data`` (issue #332) can
    compute "past interval x2" freshness. Keep these in sync with ``scripts/scheduler.sh``'s
    own ``FORAY_*_INTERVAL_HOURS`` defaults (a flat, separately-read set of env vars there -
    the scheduler predates ``Settings`` nesting and isn't worth reshaping for this alone)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ingest_hours: float = Field(gt=0, default=24)
    layers_hours: float = Field(gt=0, default=168)
    precip_hours: float = Field(gt=0, default=24)
    fire_hours: float = Field(gt=0, default=24)


class Observability(BaseModel):
    """Alerting/observability wiring (issue #332), all opt-in via env vars with a no-op
    default - a fresh checkout or a dev box with none of this configured behaves exactly as
    before (see ``foray.alerting``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    log_json: bool = False
    # Per-job healthchecks.io ping URL (e.g. {"ingest": "https://hc-ping.com/<uuid>"}),
    # JSON-encoded like `coverage`/`countries` above - healthchecks.io's UUID is the *final*
    # path component of a check's own URL, not something a shared base + job name can compose,
    # so each job that should be monitored needs its own check configured on healthchecks.io
    # and listed here. "/start"/"/fail" are appended as-is (healthchecks.io's only event
    # suffixes); a bare URL is a success ping. A job with no entry here is never pinged.
    healthchecks_urls: dict[str, str] = Field(default_factory=dict)
    # Sentry/GlitchTip DSN, shared by the API and the CLI. Empty: SDK never initialized.
    sentry_dsn: str = ""
    # Plain ntfy topic URL (e.g. "https://ntfy.sh/<topic>") that `foray alert` POSTs domain
    # alerts to. Empty: alerts are logged only, never sent.
    ntfy_url: str = ""
    # How far past its expected interval a layer can go before /healthz/data reports it stale.
    data_freshness_multiplier: float = Field(gt=1, default=2.0)
    # Debounce for the phenology rebuild (issue #332 PR 2) - the heaviest single op, previously
    # run inline after every ingest/revalidate/backfill pass. `cache.maybe_rebuild_phenology`
    # accumulates each pass's new/changed row count in `meta` and only actually rebuilds once
    # the running total crosses this. A single ingest run easily clears it on its own; this
    # mainly stops a run of small backfill ticks from each triggering their own full rebuild.
    phenology_rebuild_threshold: int = Field(gt=0, default=50)
    # Max concurrent Postgres-writing jobs (issue #332 PR 2) - `foray.jobs` gates any job
    # launched with `foray job --writer` behind this many advisory-lock "slots" so a pile-up of
    # night-window jobs starting close together can't put more than a few concurrent writers on
    # the 1-vCPU box. Start conservative, watch prod PG CPU / lock waits, raise (TODO.md #332).
    writer_cap: int = Field(gt=0, default=2)
    # TTL (issue #333 PR 2) for the in-process ranking cache (`foray.scoring.rank_cache`) that
    # sits in front of `rank_destinations`/`rank_destinations_corridor` - a backstop for cache
    # staleness beyond the explicit invalidation `build_phenology` already does on every rebuild
    # (no equivalent signal exists yet for the less-frequent trails/camps/fire cache refreshes).
    # `foray serve` runs a single uvicorn process (see `foray.cli`'s `serve` command and the
    # Dockerfile's CMD), so a module-level cache needs no cross-process invalidation.
    ranking_cache_ttl_seconds: int = Field(gt=0, default=600)


class Spaces(BaseModel):
    """DO Spaces credentials for the app's own object storage use (issue #334 PR 1) - the
    bulk-snapshot staging path (``{source}/{date}/`` under ``bulk/``) and, once configured,
    ``region_satellite``'s image/labels rasters (moved off Postgres BYTEA, a URL pointer kept
    in the table instead). Separate from the ansible-only ``foray_spaces_*`` vars that
    provision the *basemap* Space/CDN (infra/ansible/AGENTS.md) - this is the app's own runtime
    read/write access, needed by both the API process and `foray ingest-bulk`/`stage-snapshot`.
    Empty (the default) means unconfigured: `region_satellite` falls back to storing bytes in
    Postgres directly (dev-friendly, no Space required), and bulk-snapshot commands refuse to
    run - see `foray.spaces`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    access_key_id: str = ""
    secret_access_key: str = ""
    bucket: str = ""
    region: str = "nyc3"
    # Public base URL objects are read back through (the Space's own endpoint, or a CDN
    # endpoint in front of it) - e.g. "https://<bucket>.nyc3.cdn.digitaloceanspaces.com".
    # Defaults to the plain (non-CDN) Space endpoint when left empty and a bucket is set.
    public_url: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key and self.bucket)

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.region}.digitaloceanspaces.com"

    @property
    def base_url(self) -> str:
        return self.public_url or f"https://{self.bucket}.{self.region}.digitaloceanspaces.com"


class CoverageRegion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    place_id: int = Field(gt=0)
    # (west, south, east, north) lon/lat box for this region - used by the trails per-region
    # ingest (Overpass bbox filter) instead of a radius-around-a-point approximation. None for
    # regions that only need observations (place_id-based, no bbox required) - e.g. entries in
    # ``countries`` below.
    bbox: tuple[float, float, float, float] | None = None


def coverage_envelope(regions: Iterable[CoverageRegion]) -> tuple[float, float, float, float]:
    """Union ``(west, south, east, north)`` bbox of every region that has one.

    The single query envelope for a whole-coverage ingest (public land, coverage-wide
    dispersed camping). Derived from ``cfg.coverage`` rather than a hardcoded literal so
    adding regions for another country later grows it automatically. Raises ``ValueError``
    if no region carries a bbox.
    """
    boxes = [region.bbox for region in regions if region.bbox is not None]
    if not boxes:
        raise ValueError("no coverage regions with a bbox configured")
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FORAY_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    home: Home = Field(default_factory=Home)
    cell_deg: float = Field(gt=0, le=10, default=_DEFAULT_CELL_DEG)
    # URL of the Protomaps PMTiles vector basemap archive (a self-hosted US extract on DO Spaces
    # + CDN, built + uploaded by the `foray:build-basemap-once` Ansible task - see
    # docs/data-sources.md). The map has no raster fallback, so empty means no base layer.
    basemap_url: str = ""
    # Terrarium-encoded DEM tile URL template ({z}/{x}/{y}) - the source for the map's hillshade
    # + contour lines. Defaults to AWS Open Data's elevation-tiles-prod (3DEP ~10 m over CONUS,
    # free, no key, CORS-open). FORAY_TERRAIN_URL overrides it with a self-hosted URL; an empty
    # FORAY_TERRAIN_URL (a local .env) drops the terrain layer. See docs/data-sources.md.
    terrain_url: str = "https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png"
    # Relative URL template ({z}/{x}/{y}) for the full-map satellite basemap toggle (issue #340) -
    # our own proxy in front of Esri World Imagery (api/routes/tiles.py), never Esri directly, so
    # it stays same-origin under the existing CSP. Empty disables the toggle entirely.
    satellite_tiles_url: str = "/api/tiles/satellite/{z}/{x}/{y}.jpg"
    spaces: Spaces = Field(default_factory=Spaces)
    ingest: Ingest = Ingest()
    intervals: Intervals = Field(default_factory=Intervals)
    observability: Observability = Field(default_factory=Observability)
    # Sub-national regions (US states today) - the granularity trails ingest chunks by, since
    # Overpass can't handle a whole-country query in one request.
    coverage: list[CoverageRegion] = Field(default_factory=list)
    # Country-level regions - one ingest_region() call per entry covers every sub-region within
    # it in a single (paginated) query, which is both simpler and more correct than looping
    # `coverage` for data sources (like iNat observations) that don't need chunking. Adding a
    # new country later is just one more entry here, no code changes.
    countries: list[CoverageRegion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _apply_defaults(self) -> Settings:
        if not self.coverage and "coverage" not in self.model_fields_set:
            object.__setattr__(
                self,
                "coverage",
                [CoverageRegion.model_validate(entry) for entry in _DEFAULT_COVERAGE],
            )
        if not self.countries and "countries" not in self.model_fields_set:
            object.__setattr__(
                self,
                "countries",
                [CoverageRegion.model_validate(entry) for entry in _DEFAULT_COUNTRIES],
            )
        return self

    @property
    def since_year(self) -> int:
        return self.ingest.since_year

    @property
    def quality_grade(self) -> QualityGrade:
        return self.ingest.quality_grade

    @property
    def recent_weeks(self) -> int:
        return self.ingest.recent_weeks

    @property
    def region_sync_days(self) -> int:
        return self.ingest.region_sync_days
