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


def setup_logging(level: int | str | None = None) -> None:
    """Configure root logging once.

    ``level`` falls back to ``$FORAY_LOG_LEVEL`` then ``INFO``. Idempotent:
    ``logging.basicConfig`` is a no-op when the root logger already has handlers (a second
    call from the other entry point, or pytest's capture handler, wins), so this never
    stomps an existing configuration.
    """
    resolved = level if level is not None else os.environ.get("FORAY_LOG_LEVEL", _DEFAULT_LEVEL)
    logging.basicConfig(level=resolved, format=_FORMAT, datefmt=_DATEFMT)
