"""CLI tests - no network beyond the local/CI test Postgres (ingest/build functions
monkeypatched to record calls)."""

from __future__ import annotations

import json
from datetime import date

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
    """Record the ingest sequence `foray refresh` drives through `foray.refresh`."""
    seen: list[str] = []
    # `foray refresh` (non---all) delegates to foray.refresh.run_home_refresh, which resolves
    # each ingest against its own module, so patch there rather than on foray.cli.
    for target, name in (
        ("foray.refresh", "ingest"),
        ("foray.sources.camps", "ingest_campgrounds"),
        ("foray.sources.land", "ingest_public_land"),
        ("foray.sources.dispersed", "ingest_dispersed"),
        ("foray.sources.trails", "ingest_trails"),
    ):
        monkeypatch.setattr(f"{target}.{name}", lambda *args, label=name, **kwargs: seen.append(label))

    def fake_build_phenology(con, cell_deg):
        seen.append("build_phenology")
        con.execute("CREATE TABLE IF NOT EXISTS regions (region_id VARCHAR)")

    # The home-radius path rebuilds via foray.scoring.build_phenology (inside run_home_refresh);
    # the coverage-wide `--all` path calls the symbol imported into foray.cli. Patch both.
    monkeypatch.setattr("foray.scoring.build_phenology", fake_build_phenology)
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


def test_trails_force_requires_all(env_config, calls) -> None:
    result = CliRunner().invoke(cli, ["trails", "--force"])
    assert result.exit_code != 0
    assert "--force only applies to --all" in result.output
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


def test_backfill_elevation_rebuild_flag(con: psycopg.Connection, env_config, monkeypatch) -> None:
    """`--rebuild` (default) makes it *eligible* to debounce-rebuild (issue #332 PR 2's
    `maybe_rebuild_phenology`); `--no-rebuild` skips that call entirely."""
    monkeypatch.setattr(cli_module, "connect", lambda: _CloseTrackingConnection(con))
    monkeypatch.setattr(cli_module, "backfill_elevations", lambda con, *, max_points=None, cell_deg=0.25: 5)
    rebuilds: list[int] = []
    monkeypatch.setattr(
        cli_module, "maybe_rebuild_phenology", lambda con, cfg, new_rows: rebuilds.append(new_rows) or True
    )
    runner = CliRunner()

    assert runner.invoke(cli, ["backfill-elevation", "--no-rebuild"]).exit_code == 0
    assert rebuilds == []

    assert runner.invoke(cli, ["backfill-elevation"]).exit_code == 0
    assert rebuilds == [5]


def test_backfill_elevation_dem_rebuild_flag_and_exit_code(con: psycopg.Connection, env_config, monkeypatch) -> None:
    from foray.sources.elevation_dem import DemBackfillResult

    monkeypatch.setattr(cli_module, "connect", lambda: _CloseTrackingConnection(con))
    results = iter(
        [
            DemBackfillResult(
                filled=5,
                no_value=0,
                stalled=0,
                tiles_downloaded=1,
                tiles_cached=0,
                tiles_ocean=0,
                tiles_failed=0,
                remaining=0,
            ),
            DemBackfillResult(
                filled=3,
                no_value=0,
                stalled=1,
                tiles_downloaded=0,
                tiles_cached=1,
                tiles_ocean=0,
                tiles_failed=0,
                remaining=2,
            ),
        ]
    )
    monkeypatch.setattr(cli_module.elevation_dem, "backfill_elevation_dem", lambda con, **kwargs: next(results))
    rebuilds: list[int] = []
    monkeypatch.setattr(
        cli_module, "maybe_rebuild_phenology", lambda con, cfg, new_rows: rebuilds.append(new_rows) or True
    )
    runner = CliRunner()

    ok = runner.invoke(cli, ["backfill-elevation-dem"])
    assert ok.exit_code == 0
    assert rebuilds == [5]

    # A stalled batch (still eligible rows left) exits non-zero so job_runs/alerting sees it as
    # needing a re-run, even though it did make partial progress.
    stalled = runner.invoke(cli, ["backfill-elevation-dem"])
    assert stalled.exit_code != 0


def test_backfill_precip_rebuild_flag(con: psycopg.Connection, env_config, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "connect", lambda: _CloseTrackingConnection(con))
    monkeypatch.setattr(cli_module, "backfill_precip", lambda con, *, cell_deg, max_cells=None: 3)
    rebuilds: list[int] = []
    monkeypatch.setattr(
        cli_module, "maybe_rebuild_phenology", lambda con, cfg, new_rows: rebuilds.append(new_rows) or True
    )
    runner = CliRunner()

    assert runner.invoke(cli, ["backfill-precip", "--no-rebuild"]).exit_code == 0
    assert rebuilds == []
    assert runner.invoke(cli, ["backfill-precip"]).exit_code == 0
    assert rebuilds == [3]


def test_refresh_precip_cmd_reports_count(con: psycopg.Connection, env_config, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "connect", lambda: _CloseTrackingConnection(con))
    monkeypatch.setattr(cli_module, "refresh_precipitation", lambda con, cfg: 7)
    result = CliRunner().invoke(cli, ["refresh-precip"])
    assert result.exit_code == 0
    assert "7 regions" in result.output


def test_migrate_cmd_applies_schema_and_exits_clean(con: psycopg.Connection, env_config, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "connect", lambda: _CloseTrackingConnection(con))
    result = CliRunner().invoke(cli, ["migrate"])
    assert result.exit_code == 0
    assert "up to date" in result.output.lower()


def test_alert_cmd_delegates_to_alerting(env_config, monkeypatch) -> None:
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        cli_module.alerting, "alert", lambda cfg, level, message: seen.append((cfg.home.name, level, message))
    )
    result = CliRunner().invoke(cli, ["alert", "warning", "backlog growing"])
    assert result.exit_code == 0
    assert seen == [("Home", "warning", "backlog growing")]


def test_job_cmd_delegates_to_jobs_run_and_propagates_exit_code(env_config, monkeypatch) -> None:
    seen: list[tuple[str, list[str], bool]] = []
    monkeypatch.setattr(
        cli_module.jobs, "run", lambda name, argv, *, writer=False: (seen.append((name, argv, writer)), 0)[1]
    )
    result = CliRunner().invoke(cli, ["job", "fire", "--", "fire"])
    assert result.exit_code == 0
    assert seen == [("fire", ["fire"], False)]

    result = CliRunner().invoke(cli, ["job", "fire", "--writer", "--", "fire"])
    assert result.exit_code == 0
    assert seen[-1] == ("fire", ["fire"], True)

    monkeypatch.setattr(cli_module.jobs, "run", lambda name, argv, *, writer=False: 1)
    result = CliRunner().invoke(cli, ["job", "fire", "--", "fire"])
    assert result.exit_code == 1


def test_job_cmd_requires_a_wrapped_command(env_config) -> None:
    result = CliRunner().invoke(cli, ["job", "fire"])
    assert result.exit_code != 0


def test_stage_snapshot_cmd_without_spaces_configured_fails_clean(env_config) -> None:
    result = CliRunner().invoke(cli, ["stage-snapshot", "padus"])
    assert result.exit_code != 0
    assert "FORAY_SPACES" in result.output


def test_stage_snapshot_cmd_requires_exactly_one_of_source_or_all(env_config) -> None:
    result = CliRunner().invoke(cli, ["stage-snapshot"])
    assert result.exit_code != 0
    result = CliRunner().invoke(cli, ["stage-snapshot", "padus", "--all"])
    assert result.exit_code != 0


def test_stage_snapshot_cmd_all_stages_every_registered_source(env_config, monkeypatch) -> None:
    staged: list[str] = []
    monkeypatch.setattr(
        cli_module.ingest_bulk,
        "STAGERS",
        {"inat": None, "ridb": None},
    )
    monkeypatch.setattr(
        cli_module.ingest_bulk,
        "stage_snapshot",
        lambda cfg, source: staged.append(source) or date(2026, 1, 1),
    )
    result = CliRunner().invoke(cli, ["stage-snapshot", "--all"])
    assert result.exit_code == 0
    assert staged == ["inat", "ridb"]


def test_stage_snapshot_cmd_all_continues_past_a_failed_source(env_config, monkeypatch) -> None:
    # A transient failure in one source (network blip, a bad endpoint) must not prevent later
    # registered sources from being attempted - Copilot review catch on issue #357's PR. Uses a
    # plain Exception, not RuntimeError/KeyError - a real stager can raise httpx.HTTPError or a
    # botocore ClientError, neither of which the original narrow `except (KeyError, RuntimeError)`
    # would have caught (a second Copilot review catch on the same PR).
    staged: list[str] = []
    monkeypatch.setattr(cli_module.ingest_bulk, "STAGERS", {"inat": None, "ridb": None, "usfs_trails": None})

    def fake_stage(cfg, source):
        if source == "ridb":
            raise ConnectionError("boom")
        staged.append(source)
        return date(2026, 1, 1)

    monkeypatch.setattr(cli_module.ingest_bulk, "stage_snapshot", fake_stage)

    result = CliRunner().invoke(cli, ["stage-snapshot", "--all"])

    assert result.exit_code != 0
    assert staged == ["inat", "usfs_trails"]
    assert "ridb" in result.output


def test_ingest_bulk_cmd_without_spaces_configured_fails_clean(
    con: psycopg.Connection, env_config, monkeypatch
) -> None:
    monkeypatch.setattr(cli_module, "connect", lambda: _CloseTrackingConnection(con))
    result = CliRunner().invoke(cli, ["ingest-bulk", "padus"])
    assert result.exit_code != 0
    assert "FORAY_SPACES" in result.output


def test_ingest_bulk_cmd_unknown_source_fails_clean(con: psycopg.Connection, env_config, monkeypatch) -> None:
    monkeypatch.setenv("FORAY_SPACES__ACCESS_KEY_ID", "k")
    monkeypatch.setenv("FORAY_SPACES__SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("FORAY_SPACES__BUCKET", "foray-bulk")
    monkeypatch.setattr(cli_module, "connect", lambda: _CloseTrackingConnection(con))
    result = CliRunner().invoke(cli, ["ingest-bulk", "padus"])
    assert result.exit_code != 0
    assert "no loader registered" in result.output
