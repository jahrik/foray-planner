"""Tests for region-based ingest (place_id) - no network; iNat calls mocked."""

from __future__ import annotations

import json
from unittest.mock import patch

import psycopg
import pytest
from click.testing import CliRunner

from foray.cache import has_observations_in_area
from foray.cli import cli
from foray.config import CoverageRegion, Settings
from foray.inat import iter_observations
from foray.ingest import ingest_region


@pytest.fixture
def env_with_coverage(con: psycopg.Connection, monkeypatch):
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
    monkeypatch.setenv(
        "FORAY_COVERAGE",
        json.dumps([{"name": "Washington", "place_id": 46}, {"name": "Oregon", "place_id": 30}]),
    )


def _fake_obs(obs_id: int, taxon_id: int) -> dict:
    return {
        "id": obs_id,
        "taxon_id": taxon_id,
        "geojson": {"coordinates": [-122.3, 47.6]},
        "observed_on": "2024-05-15",
        "quality_grade": "research",
        "positional_accuracy": 10,
    }


def test_ingest_region_uses_place_id(con: psycopg.Connection, env_with_coverage) -> None:
    cfg = Settings()
    region = CoverageRegion(name="Washington", place_id=46)

    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([_fake_obs(1, 111), _fake_obs(2, 111)])
        counts = ingest_region(cfg, con, region)

    assert counts == {111: 2}
    mock_iter.assert_called_once()
    call_kwargs = mock_iter.call_args.kwargs
    assert call_kwargs["place_id"] == 46
    assert "lat" not in call_kwargs
    assert "lng" not in call_kwargs
    assert "radius_km" not in call_kwargs

    row = con.execute("SELECT count(*) FROM observations").fetchone()
    assert row is not None
    assert row[0] == 2

    log_row = con.execute(
        "SELECT key FROM ingest_log WHERE key LIKE %s", ["obs:111:place:46:%"]
    ).fetchone()
    assert log_row is not None


def test_ingest_region_incremental(con: psycopg.Connection, env_with_coverage) -> None:
    con.execute(
        "INSERT INTO ingest_log (key, fetched_at, row_count) "
        "VALUES ('obs:111:place:46:2024-01-01:2024-06-01', now(), 5)"
    )
    cfg = Settings()
    region = CoverageRegion(name="Washington", place_id=46)

    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([])
        ingest_region(cfg, con, region)

    call_kwargs = mock_iter.call_args.kwargs
    assert call_kwargs["d1"] >= "2024-05-25"


def test_cli_ingest_region(env_with_coverage, monkeypatch) -> None:
    runner = CliRunner()
    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([_fake_obs(10, 111)])
        result = runner.invoke(cli, ["ingest", "--region", "Washington"])

    assert result.exit_code == 0, result.output
    assert "Washington" in result.output
    assert "place_id=46" in result.output


def test_cli_ingest_all_regions(env_with_coverage, monkeypatch) -> None:
    runner = CliRunner()
    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([_fake_obs(20, 111)])
        result = runner.invoke(cli, ["ingest", "--all-regions"])

    assert result.exit_code == 0, result.output
    assert "Washington" in result.output
    assert "Oregon" in result.output


def test_cli_ingest_unknown_region(env_with_coverage) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["ingest", "--region", "Montana"])
    assert result.exit_code != 0
    assert "Unknown region" in result.output


def test_cli_ingest_region_and_all_regions_mutually_exclusive(env_with_coverage) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["ingest", "--region", "Washington", "--all-regions"])
    assert result.exit_code != 0
    assert "not both" in result.output


def test_has_observations_in_area(con: psycopg.Connection, env_with_coverage) -> None:
    assert has_observations_in_area(con, 47.6, -122.3, 50) is False

    cfg = Settings()
    region = CoverageRegion(name="Washington", place_id=46)
    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter(
            [
                _fake_obs(100, 111),
                _fake_obs(101, 111),
                _fake_obs(102, 111),
            ]
        )
        ingest_region(cfg, con, region)

    assert has_observations_in_area(con, 47.6, -122.3, 50) is False
    assert has_observations_in_area(con, 47.6, -122.3, 50, min_taxa=1) is True


def test_cli_ingest_all_regions_no_coverage(con, monkeypatch) -> None:
    monkeypatch.setenv("FORAY_HOME__LAT", "47.6")
    monkeypatch.setenv("FORAY_HOME__LNG", "-122.3")
    monkeypatch.setenv("FORAY_HOME__RADIUS_KM", "200")
    monkeypatch.setenv(
        "FORAY_SPECIES",
        json.dumps(
            [{"taxon_id": 111, "name": "Morchella", "common_name": "Morels", "rank": "genus"}]
        ),
    )
    monkeypatch.setenv("FORAY_COVERAGE", "[]")

    runner = CliRunner()
    result = runner.invoke(cli, ["ingest", "--all-regions"])
    assert result.exit_code != 0
    assert "No coverage regions configured" in result.output


@pytest.mark.parametrize(
    "kwargs",
    [
        {"taxon_id": 1, "d1": "2024-01-01", "d2": "2024-06-01"},
        {
            "taxon_id": 1,
            "lat": 47.0,
            "lng": -122.0,
            "radius_km": 50,
            "place_id": 46,
            "d1": "2024-01-01",
            "d2": "2024-06-01",
        },
    ],
    ids=["no_geo", "both_geo"],
)
def test_iter_observations_rejects_invalid_geo(kwargs) -> None:
    with pytest.raises(ValueError, match="place_id"):
        next(iter_observations(**kwargs))
