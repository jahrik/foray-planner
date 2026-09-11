"""foray.schedule - the jobs.yaml manifest loader + due-check logic behind `foray scheduler`
(issue #332 PR 2). No network; `con` is the shared test Postgres."""

from __future__ import annotations

import datetime as dt

import psycopg

from foray import schedule
from foray.cache import record_job_run


def test_load_manifest_parses_every_job_with_required_fields() -> None:
    jobs = schedule.load_manifest()
    assert jobs
    names = [job.name for job in jobs]
    assert len(names) == len(set(names))
    for job in jobs:
        assert job.command
        assert job.interval_hours > 0
        assert job.window in ("any", "night")


def test_load_manifest_includes_the_previously_missing_prod_jobs() -> None:
    """issue #332: prod cron never ran fire/precip/forage/coverage-dispersed - jobs.yaml must
    list all of them so both the dev scheduler and the systemd generator pick them up."""
    names = {job.name for job in schedule.load_manifest()}
    assert {"fire", "precip-backfill", "refresh-precip", "forage-backfill", "dispersed-coverage"} <= names


def test_night_window_jobs_have_an_interval_the_systemd_generator_can_map() -> None:
    """window: night jobs must be daily (24h) or a whole number of weeks (168h) - the systemd
    timer template picks OnCalendar daily vs. weekly off interval_hours (see
    infra/ansible/templates/foray-job.timer.j2)."""
    for job in schedule.load_manifest():
        if job.window == "night":
            assert job.interval_hours == 24 or job.interval_hours % 168 == 0


def test_due_when_never_run(con: psycopg.Connection) -> None:
    job = schedule.JobSpec(name="test-never-run", command=["fire"], interval_hours=24, window="any", writer=False)
    assert schedule._due(con, job) is True


def test_due_false_right_after_a_success(con: psycopg.Connection) -> None:
    now = dt.datetime.now(dt.UTC)
    record_job_run(con, "test-recent", started_at=now, ended_at=now, status="ok")
    job = schedule.JobSpec(name="test-recent", command=["fire"], interval_hours=24, window="any", writer=False)
    assert schedule._due(con, job) is False


def test_due_true_once_interval_elapsed(con: psycopg.Connection) -> None:
    """Also covers issue #332's E3 finding: interval math is driven by the persisted
    `job_runs` row, not an in-memory timestamp, so a scheduler restart doesn't re-run
    everything."""
    old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=25)
    record_job_run(con, "test-stale", started_at=old, ended_at=old, status="ok")
    job = schedule.JobSpec(name="test-stale", command=["fire"], interval_hours=24, window="any", writer=False)
    assert schedule._due(con, job) is True
