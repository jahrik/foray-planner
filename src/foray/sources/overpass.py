"""Shared Overpass API client for the OSM-backed layers (``dispersed``, ``trails``).

Both modules POST Overpass QL to the same endpoint and back off the same way on Overpass's
throttle (429) and server-timeout (504) responses. The QL query bodies differ per layer and
stay in their own modules; this owns the endpoint list, the region-filter fragments, and the
POST.

Endpoint resilience: ``overpass-api.de`` is the primary (biggest instance, best for the
state-sized bbox tiles ``trails`` sends), but it has bad days - and from some networks it is
simply unroutable while the mirrors are fine. ``post`` walks :data:`ENDPOINTS` in order,
moving to the next host on a connection failure, a 429, or a 5xx (a 4xx is raised straight
away - a mirror would reject a malformed query too), and only raises once every host is
spent. Override the list with ``FORAY_OVERPASS_URLS`` (comma separated) - e.g. to pin a
single instance or add a self-hosted one.
"""

from __future__ import annotations

import json
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


class ResponseTooLarge(ValueError):
    """An Overpass response grew past the caller's ``max_bytes`` cap and was abandoned unread.

    A ``ValueError`` so best-effort callers that already treat ``SOURCE_ERRORS`` as "source
    unavailable, skip" keep doing so; ``trails.ingest_trails_region`` catches it first and
    splits the tile instead. Never failed over to another mirror - it would send the same data.
    """


def _post_one(
    client: httpx.Client, url: str, query: str, *, attempts: int, base_delay: float, max_bytes: int | None
) -> dict[str, Any]:
    """POST ``query`` to one Overpass endpoint, retrying that host on 429/504.

    The body is streamed and, with ``max_bytes`` set, abandoned the moment it passes that size
    (:class:`ResponseTooLarge`) - parsing a dense 2-degree tile's response whole held >1 GB on
    the 2 GB prod droplet and got the ingest OOM-killed, so the cap has to apply before
    ``json.loads``, not after.
    """
    for attempt in range(1, attempts + 1):
        _throttle.wait()
        with client.stream("POST", url, data={"data": query}, headers={"User-Agent": USER_AGENT}) as resp:
            if resp.status_code in _RETRY_STATUS and attempt < attempts:
                delay = retry_after_seconds(resp, attempt, base_delay=base_delay)
            else:
                resp.raise_for_status()
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if max_bytes is not None and len(body) > max_bytes:
                        raise ResponseTooLarge(f"overpass: response from {url} exceeded {max_bytes} bytes")
                return json.loads(bytes(body))
        time.sleep(delay)
    raise AssertionError("unreachable: the final attempt always returns or raises")


def post(
    client: httpx.Client,
    query: str,
    *,
    attempts: int = 4,
    base_delay: float = 2.0,
    endpoints: tuple[str, ...] | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """POST an Overpass QL query, failing over across :data:`ENDPOINTS`.

    Each host gets ``attempts`` tries with 429/504 backoff (:func:`_post_one`). A connection
    failure, a 429, or a 5xx then moves to the next host; a 4xx (a malformed query, a 403) is
    raised straight away, since a mirror would answer it the same way. Raises the last
    ``httpx.HTTPError`` if every host fails, or ``ValueError`` if a 200 body isn't JSON -
    callers treat both as "source unavailable, skip". ``max_bytes`` caps the body size
    (:class:`ResponseTooLarge`, raised straight away like a 4xx).
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
    hosts = endpoints or ENDPOINTS
    if not hosts:
        raise ValueError("no Overpass endpoints configured")
    last_error: httpx.HTTPError | None = None
    for index, url in enumerate(hosts):
        try:
            return _post_one(client, url, query, attempts=attempts, base_delay=base_delay, max_bytes=max_bytes)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 429 and error.response.status_code < 500:
                raise
            last_error = error
        except httpx.TransportError as error:
            last_error = error
        if index + 1 < len(hosts):
            logger.warning("overpass: %s failed (%s) - trying next mirror", url, last_error)
    assert last_error is not None
    raise last_error
