"""Shared HTTP plumbing for the external data-source modules.

Every source (``inat``, ``elevation``, ``geocode``, ``camps``, ``dispersed``, ``trails``,
``land``) needs the same building blocks: a descriptive User-Agent, a process-wide request
pacer, ``Retry-After`` parsing, and the "log + degrade to empty" error tuple. This module
owns them so a new source (rain #226, fire #227) wires them in instead of copy-pasting.
(``inat`` keeps its own ``_with_retries`` loop - it layers pyinaturalist-specific quota
handling on top - but takes ``USER_AGENT`` from here.)
"""

from __future__ import annotations

import io
import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer

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


class HttpRangeReader(io.RawIOBase):
    """A seekable, read-only file-like view over one HTTP(S) resource, fetched lazily via
    ``Range`` requests - lets ``zipfile.ZipFile`` open a member of a multi-GB remote archive
    (issue #334 PR 2: the ~29 GB iNat GBIF DwC-A dump) without ever downloading the whole
    thing to disk. ``zipfile`` needs random access (it reads the central directory at the end
    of the file first, then seeks to the member's local header), which this provides one HTTP
    Range GET at a time; wrap it in ``io.BufferedReader(reader, buffer_size=...)`` before
    handing it to ``zipfile.ZipFile`` so zipfile's usual small reads don't turn into one
    request each - a large buffer (a few MB) keeps a sequential member scan to a modest
    request count instead of one per read() call.

    The server must support ``Accept-Ranges: bytes`` (verified against both sources this reads
    from - static.inaturalist.org and ridb.recreation.gov's downloads - not checked at
    runtime, since a server that ignores ``Range`` and 200s the whole body would silently
    corrupt every seek here rather than erroring, and both are known-good CDN-fronted static
    files, not user-supplied URLs).
    """

    def __init__(self, client: httpx.Client, url: str) -> None:
        self._client = client
        self._url = url
        self._pos = 0
        self._size = int(client.head(url, follow_redirects=True).headers["content-length"])

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._size + offset
        else:
            raise ValueError(f"invalid whence {whence!r}")
        return self._pos

    def tell(self) -> int:
        return self._pos

    def readinto(self, buffer: WriteableBuffer) -> int:
        buffer = memoryview(buffer)
        if self._pos >= self._size:
            return 0
        end = min(self._pos + len(buffer), self._size) - 1
        resp = self._client.get(self._url, headers={"Range": f"bytes={self._pos}-{end}"})
        resp.raise_for_status()
        data = resp.content
        buffer[: len(data)] = data
        self._pos += len(data)
        return len(data)
