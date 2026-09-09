"""Validate pydantic-settings configuration loading and validation rules."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from foray.config import CoverageRegion, Home, Settings


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORAY_HOME__NAME", "TestHome")
    monkeypatch.setenv("FORAY_HOME__LAT", "45.0")
    monkeypatch.setenv("FORAY_HOME__LNG", "-120.0")
    monkeypatch.setenv("FORAY_HOME__RADIUS_KM", "200")
    monkeypatch.setenv("FORAY_CELL_DEG", "0.5")
    monkeypatch.setenv("FORAY_INGEST__SINCE_YEAR", "2020")
    monkeypatch.setenv("FORAY_INGEST__QUALITY_GRADE", "research")
    monkeypatch.setenv("FORAY_INGEST__RECENT_WEEKS", "2")
    cfg = Settings()
    assert cfg.home.name == "TestHome"
    assert cfg.home.lat == 45.0
    assert cfg.home.lng == -120.0
    assert cfg.home.radius_km == 200
    assert cfg.cell_deg == 0.5
    assert cfg.since_year == 2020
    assert cfg.quality_grade == "research"
    assert cfg.recent_weeks == 2


def test_basemap_and_terrain_urls_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORAY_BASEMAP_URL", "https://cdn.example.com/us.pmtiles")
    monkeypatch.setenv("FORAY_TERRAIN_URL", "https://cdn.example.com/custom/{z}/{x}/{y}.png")
    cfg = Settings()
    assert cfg.basemap_url == "https://cdn.example.com/us.pmtiles"
    assert cfg.terrain_url == "https://cdn.example.com/custom/{z}/{x}/{y}.png"


def test_terrain_url_defaults_to_aws_open_data() -> None:
    # Unset (no env, no .env key) -> the AWS Open Data Terrarium tiles, not empty.
    assert Settings(_env_file=None).terrain_url == (
        "https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png"
    )


def test_settings_coverage_inline_json_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "FORAY_COVERAGE",
        '[{"name": "Washington", "place_id": 46}]',
    )
    cfg = Settings()
    assert len(cfg.coverage) == 1
    assert cfg.coverage[0].place_id == 46


def test_settings_defaults_applied_when_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORAY_COVERAGE", raising=False)
    monkeypatch.delenv("FORAY_COUNTRIES", raising=False)
    cfg = Settings()
    assert len(cfg.coverage) == 50
    assert len(cfg.countries) == 1
    assert cfg.countries[0].name == "United States"


def test_home_rejects_out_of_range_coordinates() -> None:
    with pytest.raises(ValidationError):
        Home(name="x", lat=200, lng=0, radius_km=100)
    with pytest.raises(ValidationError):
        Home(name="x", lat=0, lng=500, radius_km=100)


def test_home_rejects_nonpositive_radius() -> None:
    with pytest.raises(ValidationError):
        Home(name="x", lat=0, lng=0, radius_km=0)


def test_config_rejects_bad_quality_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORAY_INGEST__QUALITY_GRADE", "bogus")
    with pytest.raises(ValidationError):
        Settings()


def test_coverage_region_rejects_nonpositive_place_id() -> None:
    with pytest.raises(ValidationError):
        CoverageRegion(name="x", place_id=0)
