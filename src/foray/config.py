"""Configuration via pydantic-settings.

All settings come from environment variables (prefix ``FORAY_``, nested delimiter ``__``)
or a ``.env`` file. Complex types (species list, coverage regions) are JSON-encoded env vars.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

QualityGrade = Literal["research", "needs_id", "casual"]


class Home(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = "Home"
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    radius_km: float = Field(gt=0, le=20000)


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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FORAY_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    home: Home = Home(lat=47.6062, lng=-122.3321, radius_km=150)
    cell_deg: float = Field(gt=0, le=10, default=0.25)
    ingest: Ingest = Ingest()
    species: list[Species] = Field(default_factory=list)
    species_file: Path | None = Path("data/species_seed.json")
    coverage: list[CoverageRegion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _load_species_file(self) -> Settings:
        if not self.species and self.species_file is not None:
            path = self.species_file
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[2] / path
            raw = json.loads(path.read_text(encoding="utf-8"))
            species_list = raw if isinstance(raw, list) else raw.get("species", [])
            object.__setattr__(self, "species", [Species(**entry) for entry in species_list])
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
