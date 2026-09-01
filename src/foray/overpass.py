"""Shared Overpass API client for the OSM-backed layers (``dispersed``, ``trails``).

Both modules POST Overpass QL to the same endpoint and back off the same way on Overpass's
throttle (429) and server-timeout (504) responses. The QL query bodies differ per layer and
stay in their own modules; this owns the endpoint, the region-filter fragments, and the POST.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from foray.http import USER_AGENT, Throttle, retry_after_seconds

URL = "https://overpass-api.de/api/interpreter"

# Overpass's usage policy asks clients to keep to ~1 request/second. Shared across every caller
# in the process so a refresh that ingests dispersed camping and trails back to back still
# paces itself. dispersed.py previously defined this interval but never applied it.
_throttle = Throttle(1.0)

_RETRY_STATUS = (429, 504)


def around(lat: float, lng: float, radius_m: float) -> str:
    """An ``(around:R,lat,lng)`` filter fragment for a home-disk query."""
    return f"around:{radius_m:.0f},{lat},{lng}"


def bbox(min_lat: float, min_lng: float, max_lat: float, max_lng: float) -> str:
    """A ``(south,west,north,east)`` bounding-box filter fragment for a region-sized query."""
    return f"({min_lat},{min_lng},{max_lat},{max_lng})"


def post(client: httpx.Client, query: str, *, attempts: int = 4, base_delay: float = 2.0) -> dict[str, Any]:
    """POST an Overpass QL query, backing off on a 429/504 before giving up.

    Raises ``httpx.HTTPError`` if every attempt fails (or the final response is an error), and
    ``ValueError`` if a 200 response body isn't JSON - callers treat both as "source
    unavailable, skip".
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    resp: httpx.Response | None = None
    for attempt in range(1, attempts + 1):
        _throttle.wait()
        resp = client.post(URL, data={"data": query}, headers={"User-Agent": USER_AGENT})
        if resp.status_code in _RETRY_STATUS and attempt < attempts:
            time.sleep(retry_after_seconds(resp, attempt, base_delay=base_delay))
            continue
        break
    assert resp is not None
    resp.raise_for_status()
    return resp.json()
