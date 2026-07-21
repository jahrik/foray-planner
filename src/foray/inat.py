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
    get_taxa,
)

# Photo license codes iNat's API returns that are safe to redisplay (with attribution) under
# their terms; cc-by-nd/cc-by-nc-nd forbid derivatives (thumbnailing counts) and a null license
# means all-rights-reserved (the platform default) - those observations still get listed, just
# without a thumbnail.
DISPLAYABLE_PHOTO_LICENSES = frozenset({"cc0", "cc-by", "cc-by-sa", "cc-by-nc", "cc-by-nc-sa"})

USER_AGENT = "foray-planner/0.1 (mushroom trip planner; +https://github.com/jahrik)"

# iNat's Fungi kingdom taxon id - root of the full genus catalog (issue #79), replacing the
# old hardcoded 21-genus seed list.
FUNGI_TAXON_ID = 47170

# iNat's geoprivacy obscuration snaps a coordinate to a fixed-size grid cell, which produces a
# distinctive positional_accuracy/coordinate_uncertainty_m value - empirically this band,
# measured against foray-planner's cache (2026-07-21): 98.3% precise (4,441 true / 75 false)
# against the rows whose real `obscured` flag is already known from a live fetch. Used to
# heuristically flag likely-obscured rows that a data source doesn't carry the real flag for -
# see scripts/backfill_obscured.py (a one-time fix for the pre-existing bulk-import cache) and
# scripts/load_inat_bulk.py (applied at load time, so a *future* bulk-load doesn't reintroduce
# the same gap). Only ever a hint that a row is obscured, never proof it's precise - resync's
# live re-check is still the only path to the real flag.
OBSCURED_ACCURACY_LOW = 26000
OBSCURED_ACCURACY_HIGH = 31000

# iNat caps deep offset paging; ``id_above`` walks past that. 200 is the max page size.
_PAGE_SIZE = 200

# Transient network failures (DNS blips, timeouts, dropped connections) should not abort a
# long ingest - retry with backoff before giving up.
_TRANSIENT = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _with_retries[T](fn: Callable[[], T], *, attempts: int = 5, base_delay: float = 2.0) -> T:
    """Call ``fn``, retrying transient network errors with exponential backoff."""
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")
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
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float | None = None,
    place_id: int | None = None,
    d1: str,
    d2: str,
    quality_grade: str = "research",
) -> Iterator[dict[str, Any]]:
    """Yield every observation matching the geo + date + taxon filter.

    Walks pages by ascending id using ``id_above`` so it is not bounded by iNat's
    ~10k deep-paging limit. Supports either point+radius or place_id for geo filtering.
    """
    has_point = lat is not None and lng is not None and radius_km is not None
    if place_id is not None and has_point:
        raise ValueError("provide place_id or lat/lng/radius_km, not both")
    if place_id is None and not has_point:
        raise ValueError("provide either place_id or all of lat/lng/radius_km")

    geo_kwargs: dict[str, Any] = {}
    if place_id is not None:
        geo_kwargs["place_id"] = place_id
    else:
        geo_kwargs["lat"] = lat
        geo_kwargs["lng"] = lng
        geo_kwargs["radius"] = radius_km

    id_above = 0
    while True:
        page = _with_retries(
            lambda: get_observations(
                taxon_id=taxon_id,
                **geo_kwargs,
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


def iter_fungi_genera() -> Iterator[dict[str, Any]]:
    """Yield every genus-rank taxon under Fungi - the full catalog behind genus search/#79.

    Same ``id_above`` deep-paging idiom as ``iter_observations`` (verified live: ``/v1/taxa``
    accepts it too). ~6,018 results as of 2026-07 - well past a single page, so this always
    walks more than one request.
    """
    id_above = 0
    while True:
        page = _with_retries(
            lambda: get_taxa(
                taxon_id=FUNGI_TAXON_ID,
                rank="genus",
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
    """Return {month(1-12): observation_count} - the seasonality curve for a taxon+place."""
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


def fetch_observations(ids: list[int]) -> Iterator[dict[str, Any]]:
    """Fetch full current observation records for a batch of ids - each result's ``taxon``
    reflects iNat's identification *right now*, not whatever it was at original ingest time.

    Used by ``ingest.revalidate``/``ingest.resync`` to re-check previously-cached rows: an
    observation's identification (and therefore its genus/kingdom) can change after ingest, and
    nothing else here ever re-fetches an already-cached id outside its original ingest window.

    A generator, not a list - ``resync --until-done`` can hand this tens of thousands of ids in
    one call, and holding every page's full JSON in memory at once (verbose records: photos,
    full taxon ancestry, etc) is exactly the unbounded-memory pattern the rest of this codebase
    avoids (see ``ingest_region``'s chunked-insert comment).
    """
    for start in range(0, len(ids), _PAGE_SIZE):
        chunk = ids[start : start + _PAGE_SIZE]
        page = _with_retries(
            lambda chunk=chunk: get_observations(
                id=chunk,
                per_page=_PAGE_SIZE,
                user_agent=USER_AGENT,
            )
        )
        yield from page.get("results", [])


def photos_for_observations(ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Fetch each observation's photos (id, url, license_code, attribution), keyed by obs id.

    Only observations with at least one photo appear in the result.
    """
    if not ids:
        return {}
    photos: dict[int, list[dict[str, Any]]] = {}
    for start in range(0, len(ids), _PAGE_SIZE):
        chunk = ids[start : start + _PAGE_SIZE]
        page = _with_retries(
            lambda chunk=chunk: get_observations(
                id=chunk,
                per_page=_PAGE_SIZE,
                user_agent=USER_AGENT,
            )
        )
        for obs in page.get("results", []):
            obs_photos = obs.get("photos") or []
            if obs_photos:
                photos[obs["id"]] = obs_photos
    return photos
