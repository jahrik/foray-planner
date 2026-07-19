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
        json.dumps([{"taxon_id": 111, "name": "Morchella", "common_name": "Morels", "rank": "genus"}]),
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


def test_genera_refresh_upserts_catalog(con: psycopg.Connection, env_config, monkeypatch) -> None:
    fake_genera = [
        {"id": 47348, "name": "Cantharellus", "preferred_common_name": "Chanterelles", "observations_count": 90000},
        {"id": 999999, "name": "Obscurella", "observations_count": 3},  # no common name
    ]
    monkeypatch.setattr(cli_module, "iter_fungi_genera", lambda: iter(fake_genera))

    runner = CliRunner()
    result = runner.invoke(cli, ["genera-refresh"])

    assert result.exit_code == 0, result.output
    assert "Cached 2 Fungi genera." in result.output
    rows = con.execute("SELECT taxon_id, name, common_name FROM fungi_genera ORDER BY taxon_id").fetchall()
    assert rows == [(47348, "Cantharellus", "Chanterelles"), (999999, "Obscurella", None)]


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


class _CloseTrackingConnection:
    """Proxies to a real connection but only records close() calls rather than actually
    closing it - the wrapped connection is the shared session-scoped test fixture, which
    later tests still need open."""

    def __init__(self, real: psycopg.Connection) -> None:
        self._real = real
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def test_camps_closes_connection_on_error(con: psycopg.Connection, env_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for #89: an exception mid-command must not leak the Postgres connection."""
    tracker = _CloseTrackingConnection(con)
    monkeypatch.setattr(cli_module, "connect", lambda: tracker)

    def boom(cfg, con):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli_module, "ingest_campgrounds", boom)

    runner = CliRunner()
    result = runner.invoke(cli, ["camps"])
    assert result.exit_code != 0
    assert tracker.close_calls == 1
