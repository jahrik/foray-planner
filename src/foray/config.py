"""Configuration via pydantic-settings.

All settings come from environment variables (prefix ``FORAY_``, nested delimiter ``__``)
or a ``.env`` file. Complex types (species list, coverage regions) are JSON-encoded env vars.
Defaults for species and coverage are built into the app via ``foray.defaults``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from foray.defaults import CELL_DEG as _DEFAULT_CELL_DEG
from foray.defaults import COUNTRIES as _DEFAULT_COUNTRIES
from foray.defaults import COVERAGE as _DEFAULT_COVERAGE
from foray.defaults import HOME_LAT as _DEFAULT_HOME_LAT
from foray.defaults import HOME_LNG as _DEFAULT_HOME_LNG
from foray.defaults import HOME_RADIUS_KM as _DEFAULT_HOME_RADIUS_KM
from foray.defaults import SPECIES as _DEFAULT_SPECIES

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


class Species(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    taxon_id: int = Field(gt=0)
    name: str
    common_name: str
    rank: str

    @property
    def inat_url(self) -> str:
        return f"https://www.inaturalist.org/taxa/{self.taxon_id}"


class CoverageRegion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    place_id: int = Field(gt=0)
    # (west, south, east, north) lon/lat box for this region - used by the trails per-region
    # ingest (Overpass bbox filter) instead of a radius-around-a-point approximation. None for
    # regions that only need observations (place_id-based, no bbox required) - e.g. entries in
    # ``countries`` below.
    bbox: tuple[float, float, float, float] | None = None


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
    ingest: Ingest = Ingest()
    species: list[Species] = Field(default_factory=list)
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
        if not self.species and "species" not in self.model_fields_set:
            object.__setattr__(self, "species", [Species.model_validate(entry) for entry in _DEFAULT_SPECIES])
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
    def taxon_ids(self) -> list[int]:
        return [species.taxon_id for species in self.species]


# Backwards-compatible alias so callers can still use `Config` type annotations.
Config = Settings
