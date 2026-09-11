"""The `foray job` wrapper every scheduled command runs through (issue #332).

``scripts/scheduler.sh`` (dev) and the prod cron path call ``foray job <name> -- <foray
subcommand> [args...]`` instead of the bare subcommand directly. Each invocation:

- takes a Postgres advisory lock keyed on ``name`` so an overlapping run (the previous one
  still going when the next tick fires) skips instead of piling up - the skip is a real
  ``job_runs`` row with ``status="skipped"``, not silent like ``flock -n``'s old no-op.
- runs the wrapped command as a subprocess (a fresh ``foray`` process, so a hung/crashed job
  can't corrupt the wrapper's own state) and records disciplined exit codes: the wrapper's
  exit code mirrors the wrapped command's, 0 for a skip.
- writes exactly one ``job_runs`` row per attempt.
- pings healthchecks.io (start/success/fail) and fires a `foray alert` on failure - both
  no-ops unless configured (see ``foray.alerting``).

Each job in a `&&`-chained group in the old scheduler.sh (e.g. `backfill-precip &&
refresh-precip`) now gets its own `foray job ... --` line, so one failing doesn't block the
other from ever running.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import subprocess
import sys
import time

import psycopg

from foray import alerting
from foray.cache import connect, record_job_run
from foray.config import Settings

logger = logging.getLogger(__name__)

# A wrapped CLI command that wants its row count in `job_runs.rows` prints one of these lines
# as its last line of output (see cli.py's `emit_rows` calls) - `_run_locked` greps it out of
# the subprocess's stdout and forwards every other line unchanged, so cron logs read exactly
# as before. Not every command reports one yet (`http_429_count` isn't wired up at all - no
# source module currently counts its own 429 retries) - `rows`/`http_429_count` stay NULL/0
# for anything that doesn't call `emit_rows`.
_ROWS_PREFIX = "FORAY_JOB_ROWS="
_ROWS_RE = re.compile(rf"^{re.escape(_ROWS_PREFIX)}(\d+)$")


def emit_rows(count: int) -> None:
    """Called by a CLI command as its last action to report how many rows it processed this
    run. A plain `print` (not `click.echo`) since this is a wire-format line for `_run_locked`
    to parse, not user-facing output - it's stripped back out before the rest of that
    command's output reaches the terminal/cron log."""
    print(f"{_ROWS_PREFIX}{count}", flush=True)


def _writer_slot_key(slot: int) -> str:
    return f"writer-slot-{slot}"


def _acquire_writer_slot(con: psycopg.Connection, cap: int, name: str) -> int:
    """Block until one of ``cap`` writer-semaphore advisory-lock slots is free (issue #332 PR
    2's writer cap - at most ``cap`` jobs tagged ``writer`` run at once, so a pile-up of
    night-window jobs starting close together can't put more concurrent writers on the 1-vCPU
    box than that). Returns the slot index held; the caller releases it with
    ``pg_advisory_unlock(hashtext(_writer_slot_key(slot)))``."""
    waited = False
    while True:
        for slot in range(cap):
            got = con.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [_writer_slot_key(slot)]).fetchone()
            if got and got[0]:
                if waited:
                    logger.info("job %s: acquired writer slot %d", name, slot)
                return slot
        if not waited:
            logger.info("job %s: waiting for a free writer slot (cap=%d)", name, cap)
            waited = True
        time.sleep(5)


def run(name: str, argv: list[str], *, writer: bool = False) -> int:
    """Run ``argv`` (a ``foray`` subcommand + its own args) as job ``name``. Returns the exit
    code the caller (the ``foray job`` CLI command) should itself exit with. ``writer=True``
    blocks until a writer-cap semaphore slot is free before running (see
    ``_acquire_writer_slot``) - set for any job that writes to Postgres."""
    cfg = Settings()
    con = connect()
    writer_slot: int | None = None
    try:
        # hashtext() gives every distinct job name its own lock key without a hand-maintained
        # int table (cache.py's schema-migration lock uses a fixed literal because there's
        # only ever one of those; job names are open-ended).
        got_lock = con.execute("SELECT pg_try_advisory_lock(hashtext(%s))", [name]).fetchone()
        if not got_lock or not got_lock[0]:
            now = dt.datetime.now(dt.UTC)
            logger.warning("job %s: skipped - a previous run still holds the advisory lock", name)
            record_job_run(con, name, started_at=now, ended_at=now, status="skipped", duration_ms=0)
            return 0
        try:
            if writer:
                writer_slot = _acquire_writer_slot(con, cfg.observability.writer_cap, name)
            return _run_locked(con, cfg, name, argv)
        finally:
            if writer_slot is not None:
                con.execute("SELECT pg_advisory_unlock(hashtext(%s))", [_writer_slot_key(writer_slot)])
            con.execute("SELECT pg_advisory_unlock(hashtext(%s))", [name])
    finally:
        con.close()


def _run_locked(con: psycopg.Connection, cfg: Settings, name: str, argv: list[str]) -> int:
    started_at = dt.datetime.now(dt.UTC)
    clock_start = time.monotonic()
    alerting.healthcheck_ping(cfg, name, "start")
    logger.info("job %s: starting (%s)", name, " ".join(argv))
    # Piped (not inherited) so `emit_rows`'s line can be pulled out below - each remaining line
    # is re-printed immediately so cron's `>> log 2>&1` redirect still sees it as it happens.
    process = subprocess.Popen(
        [sys.executable, "-m", "foray.cli", *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    rows: int | None = None
    assert process.stdout is not None
    for line in process.stdout:
        match = _ROWS_RE.match(line.rstrip("\n"))
        if match:
            rows = int(match.group(1))
        else:
            print(line, end="", flush=True)
    returncode = process.wait()
    duration_ms = int((time.monotonic() - clock_start) * 1000)
    ended_at = dt.datetime.now(dt.UTC)
    status = "ok" if returncode == 0 else "error"
    record_job_run(
        con, name, started_at=started_at, ended_at=ended_at, status=status, rows=rows, duration_ms=duration_ms
    )
    if status == "ok":
        logger.info("job %s: ok (%dms)", name, duration_ms)
        alerting.healthcheck_ping(cfg, name, "")
    else:
        logger.error("job %s: failed (exit %d, %dms)", name, returncode, duration_ms)
        alerting.healthcheck_ping(cfg, name, "fail")
        alerting.alert(cfg, "error", f"job {name!r} failed (exit {returncode})")
    return returncode
