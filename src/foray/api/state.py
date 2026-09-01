"""Process-wide server state shared by every route handler and the refresh thread."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from foray.config import Settings


@dataclass
class AppState:
    """Shared server-side state, closed over by every route handler and the background
    refresh thread (see issue #91). One instance lives for the app's lifetime.

    This used to be a plain ``dict[str, Any]`` - a typo'd string key or a wrong-type write
    (e.g. an ``int`` landing in ``http_client``) only surfaced at runtime, on whatever code
    path happened to read it back. A dataclass gives ``ty`` the same static checking the
    rest of the file already gets (dataclasses, ``Home``/``Settings`` models, typed route
    return values).
    """

    cfg: Settings
    refreshing: bool = False
    last_error: str | None = None
    listeners: list[queue.Queue[dict[str, Any]]] = field(default_factory=list)
    listeners_lock: threading.Lock = field(default_factory=threading.Lock)
    last_progress: dict[str, Any] | None = None
    abort_event: threading.Event = field(default_factory=threading.Event)
    http_client: httpx.Client | None = None
    refresh_rate_limit: dict[str, float] = field(default_factory=dict)
    refresh_rate_limit_lock: threading.Lock = field(default_factory=threading.Lock)
    refresh_lock: threading.Lock = field(default_factory=threading.Lock)
