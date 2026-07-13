"""CLI tests - no network beyond the local/CI test Postgres (ingest/build functions
monkeypatched to record calls)."""

from __future__ import annotations

import json

import psycopg
import pytest
from click.testing import CliRunner

import foray.cli as cli_module
from foray.cli import cli


@pytest.fixture
def env_config(con: psycopg.Connection, monkeypatch):
    monkeypatch.setenv("FORAY_HOME__NAME", "Home")
    monkeypatch.setenv("FORAY_HOME__LAT", "47.6")
    monkeypatch.setenv("FORAY_HOME__LNG", "-122.3")
    monkeypatch.setenv("FORAY_HOME__RADIUS_KM", "200")
    monkeypatch.setenv("FORAY_CELL_DEG", "0.25")
    monkeypatch.setenv("FORAY_INGEST__SINCE_YEAR", "2015")
    monkeypatch.setenv("FORAY_INGEST__QUALITY_GRADE", "research")
    monkeypatch.setenv("FORAY_INGEST__RECENT_WEEKS", "4")
    monkeypatch.setenv(
        "FORAY_SPECIES",
        json.dumps(
            [{"taxon_id": 111, "name": "Morchella", "common_name": "Morels", "rank": "genus"}]
        ),
    )


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


def test_refresh_default_runs_everything(env_config, calls) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["refresh"])
    assert result.exit_code == 0, result.output
    assert calls == [
        "ingest",
        "ingest_campgrounds",
        "ingest_public_land",
        "ingest_dispersed",
        "ingest_trails",
        "build_phenology",
    ]


def test_refresh_with_subset_skips_others(env_config, calls) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["refresh", "--with", "camps,trails"])
    assert result.exit_code == 0, result.output
    assert calls == ["ingest_campgrounds", "ingest_trails"]
    assert "Warmed: camps, trails." in result.output


def test_refresh_with_unknown_target_errors(env_config, calls) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["refresh", "--with", "bogus"])
    assert result.exit_code != 0
    assert "unknown target" in result.output
    assert calls == []
