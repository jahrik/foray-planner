"""Configuration + species-seed loading.

Config, Home, and Species are pydantic models: config.yaml is a hand-editable trust
boundary, so values are range-validated on load with clear errors. Internal scoring types
stay plain dataclasses - pydantic is only for the file boundary. The runtime home-location
override (set from the UI) lives in Postgres (``foray.cache.load_location``/``save_location``),
not here - loading it requires a DB connection, which this module doesn't have.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

QualityGrade = Literal["research", "needs_id", "casual"]


class Home(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    radius_km: float = Field(gt=0, le=20000)


class Species(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    taxon_id: int = Field(gt=0)
    name: str
    common_name: str
    rank: str

    @property
    def inat_url(self) -> str:
        """Link to the taxon's iNaturalist page - the source of any descriptive info."""
        return f"https://www.inaturalist.org/taxa/{self.taxon_id}"


class Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    home: Home
    cell_deg: float = Field(gt=0, le=10)
    since_year: int = Field(ge=1900, le=2100)
    quality_grade: QualityGrade
    recent_weeks: int = Field(gt=0, le=520)
    species: list[Species] = Field(default_factory=list)

    @property
    def taxon_ids(self) -> list[int]:
        return [species.taxon_id for species in self.species]


def _project_root() -> Path:
    # src/foray/config.py -> project root is three parents up.
    return Path(__file__).resolve().parents[2]


def _resolve(root: Path, value: str) -> Path:
    resolved = Path(value)
    return resolved if resolved.is_absolute() else root / resolved


def load_config(path: str | Path = "config.yaml") -> Config:
    root = _project_root()
    cfg_path = _resolve(root, str(path))
    raw: dict[str, Any] = yaml.safe_load(cfg_path.read_text())

    try:
        paths = raw["paths"]
        ingest = raw["ingest"]
        species = load_species(_resolve(root, paths["species_seed"]))

        return Config(
            home=Home(**raw["home"]),
            cell_deg=raw["regions"]["cell_deg"],
            since_year=ingest["since_year"],
            quality_grade=ingest["quality_grade"],
            recent_weeks=ingest["recent_weeks"],
            species=species,
        )
    except KeyError as error:
        raise ValueError(f"missing key {error} in configuration ({cfg_path})") from error
    except ValidationError as error:
        raise ValueError(f"invalid configuration ({cfg_path}):\n{error}") from error


def load_species(path: str | Path) -> list[Species]:
    raw = yaml.safe_load(Path(path).read_text())
    try:
        return [Species(**entry) for entry in raw["species"]]
    except ValidationError as error:
        raise ValueError(f"invalid species seed ({path}):\n{error}") from error
