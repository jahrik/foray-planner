"""Look up ground elevation for coordinates via Open-Meteo's elevation API.

Free, no key required. Open-Meteo's DEM is Copernicus GLO-90 (~90 m resolution); a lookup is a
nearest-cell sample, not an interpolation. The endpoint takes up to 100 lat/lng pairs per
request, so `foray backfill-elevation` enriches observations 100 at a time. A process-wide
throttle paces a large backfill the same way `geocode._throttle` does for Nominatim.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence

import httpx

OPEN_METEO_ELEVATION = "https://api.open-meteo.com/v1/elevation"
MAX_BATCH = 100

# Open-Meteo's free tier meters the elevation endpoint per *coordinate*, not per request (a
# 100-point batch counts as 100), and 429s a burst well before the nominal 600/min. Pace by
# point count - ~0.13 s/point keeps a large backfill under ~460 points/min. A one-off backfill
# is slow but unattended; incremental ingest only enriches each run's small new delta.
_SECONDS_PER_POINT = 0.13
_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _throttle(points: int) -> None:
    global _last_request_at
    with _throttle_lock:
        wait = _last_request_at + points * _SECONDS_PER_POINT - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


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
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        _throttle(len(coords))
        resp = client.get(
            OPEN_METEO_ELEVATION,
            params={
                "latitude": ",".join(f"{lat:.6f}" for lat, _ in coords),
                "longitude": ",".join(f"{lng:.6f}" for _, lng in coords),
            },
        )
        resp.raise_for_status()
        values = resp.json().get("elevation") or []
    finally:
        if owns:
            client.close()
    result: list[int | None] = [None] * len(coords)
    for index, value in enumerate(values[: len(coords)]):
        if value is not None:
            result[index] = round(float(value))
    return result
