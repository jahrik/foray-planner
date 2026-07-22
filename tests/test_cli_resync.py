"""Tests for `foray resync`'s --until-done looping - no network; ingest.resync mocked."""

from __future__ import annotations

from unittest.mock import patch

import psycopg
from click.testing import CliRunner

from foray.cli import cli
from foray.inat import InatQuotaExceeded


def _env(monkeypatch) -> None:
    monkeypatch.setenv("FORAY_HOME__NAME", "Home")
    monkeypatch.setenv("FORAY_HOME__LAT", "47.6")
    monkeypatch.setenv("FORAY_HOME__LNG", "-122.3")
    monkeypatch.setenv("FORAY_HOME__RADIUS_KM", "200")


def test_resync_cmd_default_stops_after_one_batch(con: psycopg.Connection, monkeypatch) -> None:
    _env(monkeypatch)
    runner = CliRunner()
    with (
        patch("foray.cli.resync") as mock_resync,
        patch("foray.cli.build_phenology"),
    ):
        mock_resync.return_value = {"checked": 5, "purged": 1, "reassigned": 0}
        result = runner.invoke(cli, ["resync", "--batch-size", "5"])

    assert result.exit_code == 0, result.output
    mock_resync.assert_called_once()
    assert "5 observations checked" in result.output


def test_resync_cmd_until_done_loops_until_batch_shrinks(con: psycopg.Connection, monkeypatch) -> None:
    _env(monkeypatch)
    runner = CliRunner()
    with (
        patch("foray.cli.resync") as mock_resync,
        patch("foray.cli.build_phenology"),
    ):
        # Two full batches, then a partial (smaller-than-batch-size) final batch signals done.
        mock_resync.side_effect = [
            {"checked": 10, "purged": 0, "reassigned": 0},
            {"checked": 10, "purged": 2, "reassigned": 1},
            {"checked": 3, "purged": 0, "reassigned": 0},
        ]
        result = runner.invoke(cli, ["resync", "--batch-size", "10", "--until-done"])

    assert result.exit_code == 0, result.output
    assert mock_resync.call_count == 3
    assert "23 observations checked" in result.output
    assert "2 purged" in result.output
    assert "1 reassigned" in result.output


def test_resync_cmd_until_done_stops_on_empty_batch(con: psycopg.Connection, monkeypatch) -> None:
    _env(monkeypatch)
    runner = CliRunner()
    with (
        patch("foray.cli.resync") as mock_resync,
        patch("foray.cli.build_phenology") as mock_build,
    ):
        mock_resync.return_value = {"checked": 0, "purged": 0, "reassigned": 0}
        result = runner.invoke(cli, ["resync", "--until-done"])

    assert result.exit_code == 0, result.output
    mock_resync.assert_called_once()
    assert "Nothing to resync" in result.output
    mock_build.assert_not_called()


def test_resync_cmd_stops_cleanly_on_quota_exceeded(con: psycopg.Connection, monkeypatch) -> None:
    _env(monkeypatch)
    runner = CliRunner()
    with (
        patch("foray.cli.resync") as mock_resync,
        patch("foray.cli.build_phenology") as mock_build,
    ):
        mock_resync.side_effect = [
            {"checked": 10, "purged": 1, "reassigned": 0},
            InatQuotaExceeded(3600.0),
        ]
        result = runner.invoke(cli, ["resync", "--batch-size", "10", "--until-done"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "Stopped after 10 checked" in result.output
    assert "60 min" in result.output
    mock_build.assert_not_called()
