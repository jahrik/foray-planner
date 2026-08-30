"""Look up ground elevation for coordinates via Open-Meteo's elevation API.

Free, no key required. Open-Meteo's DEM is Copernicus GLO-90 (~90 m resolution); a lookup is a
nearest-cell sample, not an interpolation. The endpoint takes up to 100 lat/lng pairs per
request, so `foray backfill-elevation` enriches observations 100 at a time. A process-wide
throttle paces requests just under the free tier's 600/min ceiling; the hourly/daily caps show
up as 429s, which `lookup_batch` rides out with `Retry-After` backoff before giving up so the
next scheduled run can pick up where it left off.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence

import httpx

OPEN_METEO_ELEVATION = "https://api.open-meteo.com/v1/elevation"
MAX_BATCH = 100

# Open-Meteo's free tier meters the elevation endpoint per *coordinate*, not per request (a
# 100-point batch counts as 100). Pace by point count just under the published 600/min ceiling;
# the hourly and daily caps (5k / 10k) are enforced by 429s, which `lookup_batch` rides out with
# `Retry-After` backoff. A large backfill drains as fast as the free tier allows, then stops.
_SECONDS_PER_POINT = 0.11
_throttle_lock = threading.Lock()
_last_request_at = 0.0

# On a 429, retry the same batch this many times, honouring the `Retry-After` header (capped so a
# "come back at midnight UTC" hint doesn't block the run - the caller stops and the next cron
# tick retries instead).
_MAX_RETRIES = 3
_MAX_RETRY_WAIT_S = 120.0


def _throttle(points: int) -> None:
    global _last_request_at
    with _throttle_lock:
        wait = _last_request_at + points * _SECONDS_PER_POINT - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _retry_after_seconds(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429 - the ``Retry-After`` header if present and short
    enough, else exponential backoff. Capped at ``_MAX_RETRY_WAIT_S``."""
    header = resp.headers.get("Retry-After", "").strip()
    if header.isdigit():
        return min(float(header), _MAX_RETRY_WAIT_S)
    return min(2.0**attempt, _MAX_RETRY_WAIT_S)


def lookup_batch(coords: Sequence[tuple[float, float]], *, client: httpx.Client | None = None) -> list[int | None]:
    """Ground elevation in metres (rounded) for each ``(lat, lng)`` in ``coords``, in order.

    An entry is ``None`` when Open-Meteo returns no value for that point. Raises
    ``httpx.HTTPError`` on a network/HTTP failure so the caller can retry the batch later.
    ``ValueError`` if a coordinate is out of range or more than ``MAX_BATCH`` points are given.
    """
    if not coords:
        return []
    if len(coords) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} points per request, got {len(coords)}")
    for lat, lng in coords:
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError(f"coordinates out of range: {lat},{lng}")
    params = {
        "latitude": ",".join(f"{lat:.6f}" for lat, _ in coords),
        "longitude": ",".join(f"{lng:.6f}" for _, lng in coords),
    }
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        for attempt in range(_MAX_RETRIES + 1):
            _throttle(len(coords))
            resp = client.get(OPEN_METEO_ELEVATION, params=params)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                time.sleep(_retry_after_seconds(resp, attempt))
                continue
            resp.raise_for_status()
            break
        values = resp.json().get("elevation") or []
    finally:
        if owns:
            client.close()
    result: list[int | None] = [None] * len(coords)
    for index, value in enumerate(values[: len(coords)]):
        if value is not None:
            result[index] = round(float(value))
    return result
