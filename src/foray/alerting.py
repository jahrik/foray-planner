"""Best-effort alerting (issue #332): healthchecks.io dead-man's-switch pings per job, Sentry
error capture, and a plain webhook path for domain alerts (`foray alert`).

Every function here is a no-op when its config is unset (``Settings.observability``'s
defaults) - a dev box or a CI run with none of this configured behaves exactly as before.
Nothing here raises: alerting must never take down the job it's reporting on, so every
network call is wrapped and a failure is just logged.
"""

from __future__ import annotations

import logging

import httpx

from foray.config import Settings
from foray.sources.http import USER_AGENT

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0
AlertLevel = str  # "info" | "warning" | "error" - not a Literal, callers pass free text


def healthcheck_ping(cfg: Settings, job: str, event: str) -> None:
    """Ping healthchecks.io (or a compatible dead-man's-switch endpoint) for ``job``.

    ``event`` is ``"start"``, ``""`` (bare = success), or ``"fail"`` - appended as a path
    segment per healthchecks.io's convention. No-op when
    ``FORAY_OBSERVABILITY__HEALTHCHECKS_BASE_URL`` is unset.
    """
    base = cfg.observability.healthchecks_base_url
    if not base:
        return
    url = f"{base.rstrip('/')}/{job}"
    if event:
        url = f"{url}/{event}"
    try:
        httpx.get(url, timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as error:
        logger.warning("alerting: healthchecks ping for %s/%s failed (%s)", job, event or "success", error)


def alert(cfg: Settings, level: AlertLevel, message: str) -> None:
    """Deliver a domain alert (REPLACE lane wiped, backlog growing, maintenance flag left set,
    cert renewal failed, ...) to the configured ntfy topic. Always logged locally regardless
    of delivery - the log line is the alert of record when nothing is configured."""
    logger.log(_level_to_logging(level), "alert[%s]: %s", level, message)
    ntfy_url = cfg.observability.ntfy_url
    if not ntfy_url:
        return
    try:
        httpx.post(
            ntfy_url,
            content=message.encode(),
            headers={"Title": f"foray {level}", "User-Agent": USER_AGENT},
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as error:
        logger.warning("alerting: ntfy delivery failed (%s)", error)


def _level_to_logging(level: AlertLevel) -> int:
    return {"error": logging.ERROR, "warning": logging.WARNING}.get(level.lower(), logging.INFO)


def init_sentry(cfg: Settings) -> None:
    """Initialize the Sentry/GlitchTip SDK if ``FORAY_OBSERVABILITY__SENTRY_DSN`` is set and
    the ``sentry-sdk`` package is installed. Called once from the CLI group callback and from
    ``create_app`` - ``sentry_sdk.init`` is itself idempotent-safe to call more than once."""
    dsn = cfg.observability.sentry_dsn
    if not dsn:
        return
    try:
        import sentry_sdk  # ty: ignore[unresolved-import]  # optional dep, not in pyproject.toml
    except ImportError:
        logger.warning("alerting: FORAY_OBSERVABILITY__SENTRY_DSN is set but sentry-sdk isn't installed - skipping")
        return
    sentry_sdk.init(dsn=dsn)
