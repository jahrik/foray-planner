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
import subprocess
import sys
import time

import psycopg

from foray import alerting
from foray.cache import connect, record_job_run
from foray.config import Settings

logger = logging.getLogger(__name__)


def run(name: str, argv: list[str]) -> int:
    """Run ``argv`` (a ``foray`` subcommand + its own args) as job ``name``. Returns the exit
    code the caller (the ``foray job`` CLI command) should itself exit with."""
    cfg = Settings()
    con = connect()
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
            return _run_locked(con, cfg, name, argv)
        finally:
            con.execute("SELECT pg_advisory_unlock(hashtext(%s))", [name])
    finally:
        con.close()


def _run_locked(con: psycopg.Connection, cfg: Settings, name: str, argv: list[str]) -> int:
    started_at = dt.datetime.now(dt.UTC)
    clock_start = time.monotonic()
    alerting.healthcheck_ping(cfg, name, "start")
    logger.info("job %s: starting (%s)", name, " ".join(argv))
    process = subprocess.run([sys.executable, "-m", "foray.cli", *argv])
    duration_ms = int((time.monotonic() - clock_start) * 1000)
    ended_at = dt.datetime.now(dt.UTC)
    status = "ok" if process.returncode == 0 else "error"
    record_job_run(con, name, started_at=started_at, ended_at=ended_at, status=status, duration_ms=duration_ms)
    if status == "ok":
        logger.info("job %s: ok (%dms)", name, duration_ms)
        alerting.healthcheck_ping(cfg, name, "")
    else:
        logger.error("job %s: failed (exit %d, %dms)", name, process.returncode, duration_ms)
        alerting.healthcheck_ping(cfg, name, "fail")
        alerting.alert(cfg, "error", f"job {name!r} failed (exit {process.returncode})")
    return process.returncode
