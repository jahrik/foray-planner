"""``foray.jobs.run`` - the advisory-lock overlap guard, exit-code passthrough, and the
``job_runs`` row it writes on every outcome (issue #332). Runs the wrapped command as a real
subprocess (a fresh ``foray`` process) - `openapi` is used as the harmless wrapped command
here since it touches no network and no DB beyond schema application."""

from __future__ import annotations

import psycopg
import pytest

from foray import jobs
from foray.cache import connect, latest_job_run


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


def test_run_captures_rows_from_a_wrapped_commands_emit_rows_line(con: psycopg.Connection) -> None:
    """`backfill-elevation --limit 0` calls `jobs.emit_rows(0)` as its last action (see
    cli.py) with no observations to enrich and no network call - `_run_locked` should parse
    that line into `job_runs.rows` and strip it from the command's normal output."""
    exit_code = jobs.run("test-elevation-backfill", ["backfill-elevation", "--limit", "0"])

    assert exit_code == 0
    run = latest_job_run(con, "test-elevation-backfill")
    assert run is not None
    assert run["status"] == "ok"
    row = con.execute(
        "SELECT rows FROM job_runs WHERE job = %s ORDER BY started_at DESC LIMIT 1", ["test-elevation-backfill"]
    ).fetchone()
    assert row is not None
    assert row[0] == 0


def test_run_leaves_rows_null_for_a_command_that_never_calls_emit_rows(con: psycopg.Connection) -> None:
    exit_code = jobs.run("test-no-rows", ["openapi"])

    assert exit_code == 0
    row = con.execute(
        "SELECT rows FROM job_runs WHERE job = %s ORDER BY started_at DESC LIMIT 1", ["test-no-rows"]
    ).fetchone()
    assert row is not None
    assert row[0] is None


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


def test_acquire_writer_slot_returns_immediately_when_a_slot_is_free(con: psycopg.Connection) -> None:
    slot = jobs._acquire_writer_slot(con, cap=2, name="test-writer-free")
    try:
        assert slot in (0, 1)
    finally:
        con.execute("SELECT pg_advisory_unlock(hashtext(%s))", [jobs._writer_slot_key(slot)])


def test_acquire_writer_slot_waits_out_the_writer_cap(con: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """issue #332 PR 2's writer-cap semaphore: with cap=1, a held slot 0 makes
    `_acquire_writer_slot` poll (`time.sleep`) instead of returning immediately - it must pick
    the slot up as soon as it's released, not block forever."""
    holder = connect()
    got = holder.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [jobs._writer_slot_key(0)]).fetchone()
    assert got is not None and got[0] is True

    released = []

    def fake_sleep(_seconds: float) -> None:
        if not released:
            released.append(True)
            holder.execute("SELECT pg_advisory_unlock(hashtext(%s))", [jobs._writer_slot_key(0)])

    monkeypatch.setattr(jobs.time, "sleep", fake_sleep)
    try:
        slot = jobs._acquire_writer_slot(con, cap=1, name="test-writer-wait")
        assert slot == 0
        assert released == [True]
    finally:
        con.execute("SELECT pg_advisory_unlock(hashtext(%s))", [jobs._writer_slot_key(0)])
        holder.close()
