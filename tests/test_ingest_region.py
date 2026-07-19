"""Tests for region-based ingest (place_id) - no network; iNat calls mocked."""

from __future__ import annotations

import json
from unittest.mock import patch

import psycopg
import pytest
from click.testing import CliRunner

from foray.cache import upsert_fungi_genera
from foray.cli import cli
from foray.config import CoverageRegion, Settings
from foray.inat import iter_observations
from foray.ingest import ingest_region

MOREL = 111


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
        "FORAY_COVERAGE",
        json.dumps([{"name": "Washington", "place_id": 46}, {"name": "Oregon", "place_id": 30}]),
    )
    upsert_fungi_genera(con, [{"taxon_id": MOREL, "name": "Morchella", "common_name": "Morels"}])


def _fake_obs(obs_id: int, taxon_id: int, *, rank: str = "genus", ancestor_ids: list[int] | None = None) -> dict:
    """A fake iNat observation - ``taxon`` carries the ancestry ingest.py's genus resolver
    reads (see foray.ingest._resolve_genus_taxon_id): rank=="genus" uses taxon.id directly,
    otherwise the resolver looks for a known genus id in ancestor_ids."""
    return {
        "id": obs_id,
        "geojson": {"coordinates": [-122.3, 47.6]},
        "observed_on": "2024-05-15",
        "quality_grade": "research",
        "positional_accuracy": 10,
        "taxon": {"id": taxon_id, "rank": rank, "ancestor_ids": ancestor_ids or [taxon_id]},
    }


def test_ingest_region_uses_place_id(con: psycopg.Connection, env_with_coverage) -> None:
    cfg = Settings()
    region = CoverageRegion(name="Washington", place_id=46)

    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([_fake_obs(1, MOREL), _fake_obs(2, MOREL)])
        counts = ingest_region(cfg, con, region)

    assert counts == {MOREL: 2}
    mock_iter.assert_called_once()
    call_kwargs = mock_iter.call_args.kwargs
    assert call_kwargs["place_id"] == 46
    assert "lat" not in call_kwargs
    assert "lng" not in call_kwargs
    assert "radius_km" not in call_kwargs

    row = con.execute("SELECT count(*) FROM observations").fetchone()
    assert row is not None
    assert row[0] == 2

    log_row = con.execute("SELECT key FROM ingest_log WHERE key LIKE %s", ["obs:fungi:place:46:%"]).fetchone()
    assert log_row is not None


def test_ingest_region_skips_observations_with_no_known_genus_ancestor(
    con: psycopg.Connection, env_with_coverage
) -> None:
    """An observation whose ancestry doesn't intersect the fungi_genera catalog (e.g.
    subfamily-rank-or-coarser) is dropped rather than upserted with a bogus taxon_id."""
    cfg = Settings()
    region = CoverageRegion(name="Washington", place_id=46)

    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter(
            [_fake_obs(1, MOREL), _fake_obs(2, 999999, rank="family", ancestor_ids=[47170, 999999])]
        )
        counts = ingest_region(cfg, con, region)

    assert counts == {MOREL: 1}
    row = con.execute("SELECT count(*) FROM observations").fetchone()
    assert row is not None
    assert row[0] == 1


def test_ingest_region_incremental(con: psycopg.Connection, env_with_coverage) -> None:
    con.execute(
        "INSERT INTO ingest_log (key, fetched_at, row_count) "
        "VALUES ('obs:fungi:place:46:2024-01-01:2024-06-01', now(), 5)"
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
        mock_iter.return_value = iter([_fake_obs(10, MOREL)])
        result = runner.invoke(cli, ["ingest", "--region", "Washington"])

    assert result.exit_code == 0, result.output
    assert "Washington" in result.output
    assert "place_id=46" in result.output


def test_cli_ingest_all_regions(env_with_coverage, monkeypatch) -> None:
    runner = CliRunner()
    with patch("foray.ingest.iter_observations") as mock_iter:
        mock_iter.return_value = iter([_fake_obs(20, MOREL)])
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
    assert "only one of" in result.output


def test_cli_ingest_all_regions_no_coverage(con, monkeypatch) -> None:
    monkeypatch.setenv("FORAY_HOME__LAT", "47.6")
    monkeypatch.setenv("FORAY_HOME__LNG", "-122.3")
    monkeypatch.setenv("FORAY_HOME__RADIUS_KM", "200")
    monkeypatch.setenv("FORAY_COVERAGE", "[]")
    upsert_fungi_genera(con, [{"taxon_id": MOREL, "name": "Morchella", "common_name": "Morels"}])

    runner = CliRunner()
    result = runner.invoke(cli, ["ingest", "--all-regions"])
    assert result.exit_code != 0
    assert "No coverage regions configured" in result.output


def test_cli_trails_all_no_coverage_does_not_leak_connection(con, monkeypatch) -> None:
    monkeypatch.setenv("FORAY_HOME__LAT", "47.6")
    monkeypatch.setenv("FORAY_HOME__LNG", "-122.3")
    monkeypatch.setenv("FORAY_HOME__RADIUS_KM", "200")
    monkeypatch.setenv("FORAY_COVERAGE", "[]")

    runner = CliRunner()
    result = runner.invoke(cli, ["trails", "--all"])
    assert result.exit_code != 0
    assert "No coverage regions configured" in result.output


def test_cli_refresh_all_rejects_camps_and_dispersed(env_with_coverage) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["refresh", "--with", "camps,land", "--all"])
    assert result.exit_code != 0
    assert "--all doesn't apply to" in result.output


def test_cli_refresh_all_mushrooms_requires_countries(con, monkeypatch) -> None:
    monkeypatch.setenv("FORAY_HOME__LAT", "47.6")
    monkeypatch.setenv("FORAY_HOME__LNG", "-122.3")
    monkeypatch.setenv("FORAY_HOME__RADIUS_KM", "200")
    monkeypatch.setenv("FORAY_COUNTRIES", "[]")
    upsert_fungi_genera(con, [{"taxon_id": MOREL, "name": "Morchella", "common_name": "Morels"}])

    runner = CliRunner()
    result = runner.invoke(cli, ["refresh", "--with", "mushrooms", "--all"])
    assert result.exit_code != 0
    assert "No countries configured" in result.output


def test_cli_refresh_all_trails_requires_coverage(con, monkeypatch) -> None:
    monkeypatch.setenv("FORAY_HOME__LAT", "47.6")
    monkeypatch.setenv("FORAY_HOME__LNG", "-122.3")
    monkeypatch.setenv("FORAY_HOME__RADIUS_KM", "200")
    monkeypatch.setenv("FORAY_COVERAGE", "[]")

    runner = CliRunner()
    result = runner.invoke(cli, ["refresh", "--with", "trails", "--all"])
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
