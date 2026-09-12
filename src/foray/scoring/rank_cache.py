"""In-process TTL cache for the ranking path (issue #333 PR 2).

``rank_destinations`` / ``rank_destinations_corridor`` (``foray.scoring.ranking``) each run
three SQL scans over ``phenology`` (candidate totals, the target-month window, and a monthly
histogram) plus ``region_elevations`` / ``region_precip_obs`` / ``region_precip`` /
``recent_counts`` / ``_apply_fire`` (``queries.fire_near``) / ``_apply_access``
(``queries.region_access`` - a batched trailhead/campsite KNN). None of that changes between
requests unless ``phenology`` is rebuilt (``scoring.regions.build_phenology``, invoked directly
or via ``cache.maybe_rebuild_phenology``'s debounce) or the trails/camps/fire caches refresh
(less frequent, no invalidation signal available today - the TTL is the backstop for those).

A plain module-level dict is safe here because ``foray serve`` runs a single uvicorn process -
no multi-worker, confirmed via ``foray.cli``'s ``serve`` command and the Dockerfile's
``CMD ["foray", "serve", ...]`` - so there's no cross-process invalidation problem, just an
in-process race between concurrent request-handling threads, which the lock below covers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foray.scoring.models import RegionScore

# Keep in sync with Settings.observability.ranking_cache_ttl_seconds' default (foray.config).
DEFAULT_TTL_SECONDS = 600.0

CacheKey = tuple[object, ...]

_lock = threading.Lock()
_store: dict[CacheKey, tuple[float, list[RegionScore]]] = {}


def get(key: CacheKey, ttl_seconds: float) -> list[RegionScore] | None:
    """A cache hit for ``key`` still inside ``ttl_seconds``, else ``None``.

    Returns a shallow per-item copy (``dataclasses.replace``) rather than the stored list
    itself, so a caller that later mutated a returned ``RegionScore`` in place couldn't corrupt
    what a future hit hands back. Nothing on the current call paths does that (``_apply_fire``/
    ``_apply_access`` mutate in place, but only *inside* ``rank_destinations``, before the
    result is cached) - this is just cheap insurance against a future route-level change.
    """
    with _lock:
        entry = _store.get(key)
    if entry is None:
        return None
    inserted_at, results = entry
    if time.monotonic() - inserted_at > ttl_seconds:
        return None
    return [replace(region) for region in results]


def put(key: CacheKey, results: list[RegionScore]) -> None:
    with _lock:
        _store[key] = (time.monotonic(), results)


def invalidate() -> None:
    """Drop every cached ranking result. Called after a phenology rebuild cutover
    (``scoring.regions._build_phenology_locked``) - the only thing that can change what these
    keys resolve to sooner than their TTL."""
    with _lock:
        _store.clear()


def radial_key(
    *,
    months: tuple[int, ...],
    taxon_ids: tuple[int, ...],
    home_lat: float,
    home_lng: float,
    radius_km: float,
    cell_deg: float,
    recent_weeks: int,
) -> CacheKey:
    """Cache key for ``rank_destinations``. Coordinates/radius are rounded to ~3 decimals
    (~100 m) so float noise in a repeated request doesn't fragment the cache - cards are
    effectively identical at that precision."""
    return (
        "radial",
        months,
        taxon_ids,
        round(home_lat, 3),
        round(home_lng, 3),
        round(radius_km, 3),
        cell_deg,
        recent_weeks,
    )


def corridor_key(
    *,
    months: tuple[int, ...],
    taxon_ids: tuple[int, ...],
    start_lat: float,
    start_lng: float,
    dest_lat: float,
    dest_lng: float,
    corridor_km: float,
    cell_deg: float,
    recent_weeks: int,
) -> CacheKey:
    """Cache key for ``rank_destinations_corridor``. Same rounding rationale as ``radial_key``."""
    return (
        "corridor",
        months,
        taxon_ids,
        round(start_lat, 3),
        round(start_lng, 3),
        round(dest_lat, 3),
        round(dest_lng, 3),
        round(corridor_km, 3),
        cell_deg,
        recent_weeks,
    )
