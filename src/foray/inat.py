"""Thin, throttled wrapper around pyinaturalist.

Only the calls this project needs. pyinaturalist already throttles (requests-ratelimiter,
1 req/s by default) and caches (requests-cache), which keeps us well under iNat's limits.
We add a descriptive User-Agent for attribution/ToS and deep pagination via ``id_above``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import requests.exceptions
from pyinaturalist import (
    get_observation_histogram,
    get_observation_species_counts,
    get_observations,
)

USER_AGENT = "foray-planner/0.1 (mushroom trip planner; +https://github.com/jahrik)"

# iNat caps deep offset paging; ``id_above`` walks past that. 200 is the max page size.
_PAGE_SIZE = 200

# Transient network failures (DNS blips, timeouts, dropped connections) should not abort a
# long ingest — retry with backoff before giving up.
_TRANSIENT = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _with_retries[T](fn: Callable[[], T], *, attempts: int = 5, base_delay: float = 2.0) -> T:
    """Call ``fn``, retrying transient network errors with exponential backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except _TRANSIENT:
            if attempt == attempts:
                raise
            time.sleep(base_delay * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


def iter_observations(
    *,
    taxon_id: int | list[int],
    lat: float,
    lng: float,
    radius_km: float,
    d1: str,
    d2: str,
    quality_grade: str = "research",
) -> Iterator[dict[str, Any]]:
    """Yield every observation matching the geo + date + taxon filter.

    Walks pages by ascending id using ``id_above`` so it is not bounded by iNat's
    ~10k deep-paging limit.
    """
    id_above = 0
    while True:
        page = _with_retries(
            lambda: get_observations(
                taxon_id=taxon_id,
                lat=lat,
                lng=lng,
                radius=radius_km,
                d1=d1,
                d2=d2,
                quality_grade=quality_grade,
                per_page=_PAGE_SIZE,
                order_by="id",
                order="asc",
                id_above=id_above,  # noqa: B023 (sync loop; lambda called immediately)
                user_agent=USER_AGENT,
            )
        )
        results = page.get("results", [])
        if not results:
            return
        yield from results
        if len(results) < _PAGE_SIZE:
            return
        id_above = results[-1]["id"]


def species_counts(
    *,
    lat: float,
    lng: float,
    radius_km: float,
    taxon_id: int | list[int] | None = None,
    d1: str | None = None,
    d2: str | None = None,
    month: int | list[int] | None = None,
    quality_grade: str = "research",
) -> list[dict[str, Any]]:
    """Ranked species leaderboard for a geo/time filter (iNat aggregates this server-side)."""
    resp = get_observation_species_counts(
        taxon_id=taxon_id,
        lat=lat,
        lng=lng,
        radius=radius_km,
        d1=d1,
        d2=d2,
        month=month,
        quality_grade=quality_grade,
        user_agent=USER_AGENT,
    )
    return resp.get("results", [])


def monthly_histogram(
    *,
    taxon_id: int | list[int],
    lat: float,
    lng: float,
    radius_km: float,
    quality_grade: str = "research",
) -> dict[int, int]:
    """Return {month(1-12): observation_count} — the seasonality curve for a taxon+place."""
    resp = get_observation_histogram(
        taxon_id=taxon_id,
        lat=lat,
        lng=lng,
        radius=radius_km,
        date_field="observed",
        interval="month_of_year",
        quality_grade=quality_grade,
        user_agent=USER_AGENT,
    )
    # keys come back as month numbers (as ints or strings depending on version)
    return {int(k): int(v) for k, v in resp.items()}
