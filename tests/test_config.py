"""Guard the real config.yaml + species_seed.yaml actually parse and are well-formed."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foray.config import Config, Home, Species, load_config


def test_real_config_and_seed_parse() -> None:
    # Regression: the seed file must parse (colons in notes must be quoted).
    cfg = load_config()
    assert cfg.home.radius_km > 0
    assert cfg.cell_deg > 0
    assert cfg.species, "seed list should not be empty"

    taxon_ids = [species.taxon_id for species in cfg.species]
    assert len(taxon_ids) == len(set(taxon_ids)), "duplicate taxon_ids in seed"
    for species in cfg.species:
        assert species.taxon_id > 0
        assert species.common_name
        assert species.inat_url.endswith(f"/{species.taxon_id}")


def test_home_rejects_out_of_range_coordinates() -> None:
    with pytest.raises(ValidationError):
        Home(name="x", lat=200, lng=0, radius_km=100)
    with pytest.raises(ValidationError):
        Home(name="x", lat=0, lng=500, radius_km=100)


def test_home_rejects_nonpositive_radius() -> None:
    with pytest.raises(ValidationError):
        Home(name="x", lat=0, lng=0, radius_km=0)


def test_species_forbids_unknown_fields() -> None:
    # Guards against silent typos in the seed file. model_validate takes a mapping, so the
    # deliberately bad field is validated at runtime rather than flagged by the type checker.
    with pytest.raises(ValidationError):
        Species.model_validate(
            {"taxon_id": 1, "name": "a", "common_name": "b", "rank": "species", "note": "x"}
        )


def test_config_rejects_bad_quality_grade() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "home": {"name": "somewhere", "lat": 0, "lng": 0, "radius_km": 100},
                "cell_deg": 0.5,
                "since_year": 2015,
                "quality_grade": "bogus",
                "recent_weeks": 4,
            }
        )
