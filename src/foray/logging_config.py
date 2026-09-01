"""One place to configure logging for both entry points.

The CLI (``foray ...``) and the API server (``foray serve`` / uvicorn) both want ``foray.*``
progress logs on stderr in a consistent format. Previously only the CLI called
``logging.basicConfig``, so running the server gave the ``foray`` namespace Python's default
(WARNING, bare format). ``setup_logging`` is idempotent - safe to call from the Click group
callback and from ``create_app`` both.
"""

from __future__ import annotations

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


def setup_logging(level: int | str | None = None) -> None:
    """Configure root logging once, at :func:`resolve_level`'s result.

    Idempotent: ``logging.basicConfig`` is a no-op when the root logger already has handlers
    (a second call from the other entry point, or pytest's capture handler, wins), so this
    never stomps an existing configuration.
    """
    logging.basicConfig(level=resolve_level(level), format=_FORMAT, datefmt=_DATEFMT)
