"""CLI tests - no network (ingest/build functions monkeypatched to record calls)."""

from __future__ import annotations

import duckdb
import pytest
from click.testing import CliRunner

import foray.cli as cli_module
from foray.cache import SCHEMA
from foray.cli import cli


@pytest.fixture
def config_file(tmp_path):
    db_path = tmp_path / "foray.duckdb"
    duckdb.connect(str(db_path)).execute(SCHEMA)
    species_seed = tmp_path / "species_seed.yaml"
    species_seed.write_text(
        """
species:
  - taxon_id: 111
    name: Morchella
    common_name: Morels
    rank: genus
"""
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
home:
  name: Home
  lat: 47.6
  lng: -122.3
  radius_km: 200
regions:
  cell_deg: 0.25
ingest:
  since_year: 2015
  quality_grade: research
  recent_weeks: 4
paths:
  db: {db_path}
  species_seed: {species_seed}
"""
    )
    return config_path


@pytest.fixture
def calls(monkeypatch):
    seen: list[str] = []
    for name in (
        "ingest",
        "ingest_campgrounds",
        "ingest_public_land",
        "ingest_dispersed",
        "ingest_trails",
    ):
        monkeypatch.setattr(cli_module, name, lambda cfg, con, n=name: seen.append(n))

    def fake_build_phenology(con, cell_deg):
        seen.append("build_phenology")
        con.execute("CREATE TABLE IF NOT EXISTS regions (region_id VARCHAR)")

    monkeypatch.setattr(cli_module, "build_phenology", fake_build_phenology)
    return seen


def test_refresh_default_runs_everything(config_file, calls) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--config", str(config_file), "refresh"])
    assert result.exit_code == 0, result.output
    assert calls == [
        "ingest",
        "ingest_campgrounds",
        "ingest_public_land",
        "ingest_dispersed",
        "ingest_trails",
        "build_phenology",
    ]


def test_refresh_with_subset_skips_others(config_file, calls) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--config", str(config_file), "refresh", "--with", "camps,trails"])
    assert result.exit_code == 0, result.output
    assert calls == ["ingest_campgrounds", "ingest_trails"]
    assert "Warmed: camps, trails." in result.output


def test_refresh_with_unknown_target_errors(config_file, calls) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--config", str(config_file), "refresh", "--with", "bogus"])
    assert result.exit_code != 0
    assert "unknown target" in result.output
    assert calls == []
