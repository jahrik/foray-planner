"""One place to configure logging for both entry points.

The CLI (``foray ...``) and the API server (``foray serve`` / uvicorn) both want ``foray.*``
progress logs on stderr in a consistent format. Previously only the CLI called
``logging.basicConfig``, so running the server gave the ``foray`` namespace Python's default
(WARNING, bare format). ``setup_logging`` is idempotent - safe to call from the Click group
callback and from ``create_app`` both.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os

_DEFAULT_LEVEL = "INFO"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def resolve_level(level: int | str | None) -> int:
    """Turn ``level`` (or ``$FORAY_LOG_LEVEL``, or the ``INFO`` default) into a numeric level.

    A string is case-insensitive (``debug`` == ``DEBUG``); an unrecognised or empty value
    falls back to ``INFO`` rather than raising.
    """
    if level is None:
        level = os.environ.get("FORAY_LOG_LEVEL") or _DEFAULT_LEVEL
    if isinstance(level, int):
        return level
    numeric = logging.getLevelName(level.strip().upper())
    return numeric if isinstance(numeric, int) else logging.INFO


def _use_json() -> bool:
    """``FORAY_OBSERVABILITY__LOG_JSON`` (pydantic-settings' nested-delimiter name), read
    directly rather than through ``Settings`` - logging is configured before a ``Settings()``
    exists (the CLI group callback, ``create_app`` before ``cfg`` is resolved) and must never
    fail on a bad env value the way pydantic validation could."""
    return (os.environ.get("FORAY_OBSERVABILITY__LOG_JSON") or "").strip().lower() in ("1", "true", "yes")


class _JsonFormatter(logging.Formatter):
    """Structured JSON logs (issue #332) - one object per line, so a log aggregator or
    `jq`/`duckdb_query` can filter without a `%(message)s` regex. Same fields a plain-text
    line already carries (timestamp, level, logger name, message) plus ``exc_info`` when
    present; extra attributes passed via ``logger.info(..., extra={...})`` ride along too."""

    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update((key, value) for key, value in record.__dict__.items() if key not in self._RESERVED)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: int | str | None = None) -> None:
    """Configure root logging once, at :func:`resolve_level`'s result.

    Idempotent: ``logging.basicConfig`` is a no-op when the root logger already has handlers
    (a second call from the other entry point, or pytest's capture handler, wins), so this
    never stomps an existing configuration. ``FORAY_OBSERVABILITY__LOG_JSON`` (unset by
    default) switches the formatter from the plain-text line above to one JSON object per
    line - meant for the cron/prod environment, not local dev's terminal.
    """
    if _use_json():
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logging.basicConfig(level=resolve_level(level), handlers=[handler])
    else:
        logging.basicConfig(level=resolve_level(level), format=_FORMAT, datefmt=_DATEFMT)
