"""The dev-loop scheduler (issue #332 PR 2): reads ``jobs.yaml``, the single manifest also used
to generate the prod systemd timers (``infra/ansible/tasks/deploy/systemd_jobs.yml``), and runs
each job on its own interval via ``foray job`` - the direct replacement for the old
``scripts/scheduler.sh`` (a hand-maintained shell loop that drifted from prod's separately
hand-maintained cron job list; see TODO.md's E1).

Unlike the old script, this doesn't track ``*_last`` timestamps in memory - a restart used to
mean every job looked overdue and fired at once. Instead each tick asks Postgres for the job's
last successful run (``cache.latest_successful_job_run``, already recorded by every `foray job`
invocation) and compares that against ``interval_hours``, so a container restart resumes the
real schedule instead of re-running everything.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

from foray import jobs
from foray.cache import connect, latest_successful_job_run

logger = logging.getLogger(__name__)

# jobs.yaml lives at the repo root; schedule.py is src/foray/schedule.py, so two parents up
# lands at the repo root both in a checkout and in the runtime image (Dockerfile's `COPY . /app`
# then `WORKDIR /app`).
_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "jobs.yaml"

Window = Literal["any", "night"]


@dataclass(frozen=True)
class JobSpec:
    name: str
    command: list[str]
    interval_hours: float
    window: Window
    writer: bool


def load_manifest(path: Path = _MANIFEST_PATH) -> list[JobSpec]:
    data = yaml.safe_load(path.read_text())
    return [
        JobSpec(
            name=entry["name"],
            command=str(entry["command"]).split(),
            interval_hours=float(entry["interval_hours"]),
            window=entry.get("window", "any"),
            writer=bool(entry.get("writer", False)),
        )
        for entry in data["jobs"]
    ]


def _due(con, job: JobSpec) -> bool:
    """A job is due once ``interval_hours`` has elapsed since its last *successful* run (or it
    has never run). ``window: night`` jobs are additionally gated by the caller (see
    ``run_scheduler``) - this only answers the interval question."""
    last = latest_successful_job_run(con, job.name)
    if last is None:
        return True
    elapsed_hours = (_utcnow() - last["started_at"]).total_seconds() / 3600
    return elapsed_hours >= job.interval_hours


def _utcnow():
    import datetime as dt

    return dt.datetime.now(dt.UTC)


def _in_night_window() -> bool:
    """02:00-05:00 America/Los_Angeles (TODO.md O2) - coverage-wide/heavy jobs only start in
    this low-traffic window. Systemd enforces this for real on prod via OnCalendar; this dev
    loop applies the same gate so local/dev behavior matches (feedback_dev_must_mirror_prod)."""
    from zoneinfo import ZoneInfo

    now_pt = _utcnow().astimezone(ZoneInfo("America/Los_Angeles"))
    return 2 <= now_pt.hour < 5


def run_scheduler(*, poll_seconds: int = 300) -> None:
    manifest = load_manifest()
    logger.info("scheduler: loaded %d jobs from %s", len(manifest), _MANIFEST_PATH)
    while True:
        con = connect()
        try:
            for job in manifest:
                if job.window == "night" and not _in_night_window():
                    continue
                if not _due(con, job):
                    continue
                logger.info("scheduler: starting %s (%s)", job.name, " ".join(job.command))
                exit_code = jobs.run(job.name, job.command, writer=job.writer)
                if exit_code:
                    logger.warning("scheduler: %s exited %d", job.name, exit_code)
        finally:
            con.close()
        time.sleep(poll_seconds)
