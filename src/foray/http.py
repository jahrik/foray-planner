"""Shared HTTP plumbing for the external data-source modules.

Every source (``inat``, ``elevation``, ``geocode``, ``camps``, ``dispersed``, ``trails``,
``land``) needs the same building blocks: a descriptive User-Agent, a process-wide request
pacer, ``Retry-After`` parsing, and the "log + degrade to empty" error tuple. This module
owns them so a new source (rain #226, fire #227) wires them in instead of copy-pasting.
(``inat`` keeps its own ``_with_retries`` loop - it layers pyinaturalist-specific quota
handling on top - but takes ``USER_AGENT`` from here.)
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

# Attribution / ToS: iNat and Nominatim both ask for a descriptive UA identifying the app.
USER_AGENT = "foray-planner/0.1 (mushroom trip planner; +https://github.com/jahrik)"

# The copy-pasted "log the failure and return []" tuple in land / trails / dispersed: a
# transport error (``httpx.HTTPError``), or a service returning something that isn't the
# well-formed JSON we expect (a decode error - ``ValueError`` - or an unexpected shape -
# ``KeyError`` / ``TypeError``). Best-effort context sources catch this and degrade to empty
# rather than aborting the whole refresh.
SOURCE_ERRORS = (httpx.HTTPError, ValueError, KeyError, TypeError)


class Throttle:
    """Process-wide minimum-interval request pacer.

    ``wait()`` blocks until at least ``min_interval`` seconds (times ``units``, for endpoints
    metered per-item rather than per-request - see ``elevation``) have passed since the last
    call, then records "now" as the new last-call time. A single lock serialises callers, so a
    burst of concurrent FastAPI requests degrades to one call per interval instead of a
    thundering herd. ``min_interval`` is a plain attribute so tests can zero it out.
    """

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def wait(self, units: float = 1.0) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            gap = self._last_request_at + self.min_interval * units - time.monotonic()
            if gap > 0:
                time.sleep(gap)
            self._last_request_at = time.monotonic()


def retry_after_seconds(response: httpx.Response, attempt: int, *, base_delay: float = 2.0, cap: float = 60.0) -> float:
    """Seconds to wait before retrying a throttled response.

    The ``Retry-After`` header if present and parseable - either the delta-seconds form
    (``"12"``, ``"12.5"``) or the HTTP-date form - otherwise exponential backoff
    (``base_delay * 2**(attempt-1)``). Always clamped to ``[0, cap]`` so a "come back at
    midnight UTC" hint can't stall the run; the caller gives up and the next scheduled tick
    retries instead.
    """
    header = response.headers.get("Retry-After", "").strip()
    if header:
        try:
            return _clamp(float(header), cap)
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            retry_at = None
        if retry_at is not None:
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return _clamp((retry_at - datetime.now(UTC)).total_seconds(), cap)
    return _clamp(base_delay * 2 ** (attempt - 1), cap)


def _clamp(value: float, cap: float) -> float:
    return min(max(value, 0.0), cap)
