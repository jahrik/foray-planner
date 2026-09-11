"""``foray.jobs.run`` - the advisory-lock overlap guard, exit-code passthrough, and the
``job_runs`` row it writes on every outcome (issue #332). Runs the wrapped command as a real
subprocess (a fresh ``foray`` process) - `openapi` is used as the harmless wrapped command
here since it touches no network and no DB beyond schema application."""

from __future__ import annotations

import psycopg

from foray import jobs
from foray.cache import latest_job_run


def test_run_records_an_ok_row_and_returns_zero(con: psycopg.Connection) -> None:
    exit_code = jobs.run("test-openapi", ["openapi"])

    assert exit_code == 0
    run = latest_job_run(con, "test-openapi")
    assert run is not None
    assert run["status"] == "ok"


def test_run_records_an_error_row_and_returns_nonzero_on_a_bad_command(con: psycopg.Connection) -> None:
    exit_code = jobs.run("test-bad-command", ["not-a-real-command"])

    assert exit_code != 0
    run = latest_job_run(con, "test-bad-command")
    assert run is not None
    assert run["status"] == "error"


def test_run_skips_when_the_advisory_lock_is_already_held(con: psycopg.Connection) -> None:
    got = con.execute("SELECT pg_try_advisory_lock(hashtext(%s))", ["held-job"]).fetchone()
    assert got is not None and got[0] is True
    try:
        exit_code = jobs.run("held-job", ["openapi"])
    finally:
        con.execute("SELECT pg_advisory_unlock(hashtext(%s))", ["held-job"])

    assert exit_code == 0
    run = latest_job_run(con, "held-job")
    assert run is not None
    assert run["status"] == "skipped"
