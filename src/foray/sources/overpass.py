"""Shared Overpass API client for the OSM-backed layers (``dispersed``, ``trails``).

Both modules POST Overpass QL to the same endpoint and back off the same way on Overpass's
throttle (429) and server-timeout (504) responses. The QL query bodies differ per layer and
stay in their own modules; this owns the endpoint list, the region-filter fragments, and the
POST.

Endpoint resilience: ``overpass-api.de`` is the primary (biggest instance, best for the
state-sized bbox tiles ``trails`` sends), but it has bad days - and from some networks it is
simply unroutable while the mirrors are fine. ``post`` walks :data:`ENDPOINTS` in order,
moving to the next host on a connection failure or an exhausted 429/504 retry, and only
raises once every host is spent. Override the list with ``FORAY_OVERPASS_URLS`` (comma
separated) - e.g. to pin a single instance or add a self-hosted one.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from foray.sources.http import USER_AGENT, Throttle, retry_after_seconds

logger = logging.getLogger(__name__)

# overpass-api.de first (largest capacity, longest server-side timeouts); the rest are public
# mirrors used only when it fails. All speak the same Overpass QL and JSON.
_DEFAULT_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)


def _configured_endpoints() -> tuple[str, ...]:
    """The endpoint list, from ``FORAY_OVERPASS_URLS`` (comma separated) or the default."""
    raw = os.environ.get("FORAY_OVERPASS_URLS", "").strip()
    if raw:
        return tuple(url.strip() for url in raw.split(",") if url.strip())
    return _DEFAULT_ENDPOINTS


ENDPOINTS = _configured_endpoints()

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


def _post_one(client: httpx.Client, url: str, query: str, *, attempts: int, base_delay: float) -> dict[str, Any]:
    """POST ``query`` to one Overpass endpoint, retrying that host on 429/504."""
    resp: httpx.Response | None = None
    for attempt in range(1, attempts + 1):
        _throttle.wait()
        resp = client.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT})
        if resp.status_code in _RETRY_STATUS and attempt < attempts:
            time.sleep(retry_after_seconds(resp, attempt, base_delay=base_delay))
            continue
        break
    assert resp is not None
    resp.raise_for_status()
    return resp.json()


def post(
    client: httpx.Client,
    query: str,
    *,
    attempts: int = 4,
    base_delay: float = 2.0,
    endpoints: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """POST an Overpass QL query, failing over across :data:`ENDPOINTS`.

    Each host gets ``attempts`` tries with 429/504 backoff (:func:`_post_one`); a connection
    failure or an exhausted retry moves to the next host. Raises the last ``httpx.HTTPError``
    if every host fails, or ``ValueError`` if a 200 body isn't JSON - callers treat both as
    "source unavailable, skip".
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    hosts = endpoints or ENDPOINTS
    if not hosts:
        raise ValueError("no Overpass endpoints configured")
    last_error: httpx.HTTPError | None = None
    for index, url in enumerate(hosts):
        try:
            return _post_one(client, url, query, attempts=attempts, base_delay=base_delay)
        except httpx.HTTPError as error:
            last_error = error
            if index + 1 < len(hosts):
                logger.warning("overpass: %s failed (%s) - trying next mirror", url, error)
    assert last_error is not None
    raise last_error
