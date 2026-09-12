"""In-process TTL cache for the ranking path (issue #333 PR 2).

``rank_destinations`` / ``rank_destinations_corridor`` (``foray.scoring.ranking``) each run
three SQL scans over ``phenology`` (candidate totals, the target-month window, and a monthly
histogram) plus ``region_elevations`` / ``region_precip_obs`` / ``region_precip`` /
``recent_counts`` / ``_apply_fire`` (``queries.fire_near``) / ``_apply_access``
(``queries.region_access`` - a batched trailhead/campsite KNN). None of that changes between
requests unless ``phenology`` is rebuilt (``scoring.regions.build_phenology``, invoked directly
or via ``cache.maybe_rebuild_phenology``'s debounce) or the trails/camps/land/fire caches are
written (``cache._invalidate_rank_cache``'s call sites) - the TTL is the backstop for anything
that writes those tables outside ``cache.py``'s own helpers.

A plain module-level dict is safe here because ``foray serve`` runs a single uvicorn process -
no multi-worker, confirmed via ``foray.cli``'s ``serve`` command and the Dockerfile's
``CMD ["foray", "serve", ...]`` - so there's no cross-process invalidation problem, just an
in-process race between concurrent request-handling threads, which the lock below covers.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foray.scoring.models import RegionScore

# Keep in sync with Settings.observability.ranking_cache_ttl_seconds' default (foray.config).
DEFAULT_TTL_SECONDS = 600.0

# Bounds total memory (Copilot review, PR #348: an unbounded dict keyed on every distinct
# (mode, months, taxa, coords, radius, cell_deg, recent_weeks) combination a busy box ever saw
# would never shrink on its own). Each entry holds one ranked region list - typically tens to a
# couple hundred `RegionScore` objects - so 500 entries is a generous working set for a
# single-device-at-a-time app, evicted least-recently-*inserted-or-hit* first.
_MAX_ENTRIES = 500

CacheKey = tuple[object, ...]

_lock = threading.Lock()
# OrderedDict for LRU eviction: `get` moves a hit to the end, `put` evicts from the front once
# over `_MAX_ENTRIES`.
_store: OrderedDict[CacheKey, tuple[float, list[RegionScore]]] = OrderedDict()
# Bumped by every `invalidate()`. `put()` only accepts a result computed under the generation
# that was current when its caller started (see `current_generation`) - otherwise a ranking call
# already in flight when a write invalidates the cache would repopulate it with the now-stale
# result it was midway through computing (Copilot review, PR #348).
_generation = 0


def current_generation() -> int:
    """The invalidation generation right now. Callers capture this *before* running their own
    query (right after a `get()` miss), then pass it back to `put()` - see that function."""
    with _lock:
        return _generation


def get(key: CacheKey, ttl_seconds: float) -> list[RegionScore] | None:
    """A cache hit for ``key`` still inside ``ttl_seconds``, else ``None``.

    An expired entry is deleted here rather than left for a future overwrite (Copilot review,
    PR #348) - otherwise a key nothing ever requests again would sit in ``_store`` forever
    despite being logically gone. Returns a shallow per-item copy (``dataclasses.replace``)
    rather than the stored list itself, so a caller that later mutated a returned ``RegionScore``
    in place couldn't corrupt what a future hit hands back. Nothing on the current call paths
    does that (``_apply_fire``/``_apply_access`` mutate in place, but only *inside*
    ``rank_destinations``, before the result is cached) - this is just cheap insurance against a
    future route-level change.
    """
    with _lock:
        entry = _store.get(key)
        if entry is None:
            return None
        inserted_at, results = entry
        if time.monotonic() - inserted_at > ttl_seconds:
            del _store[key]
            return None
        _store.move_to_end(key)
    return [replace(region) for region in results]


def put(key: CacheKey, results: list[RegionScore], generation: int) -> None:
    """Cache ``results`` under ``key`` - unless ``generation`` (from ``current_generation()``,
    captured before the caller started computing ``results``) is no longer current, meaning an
    ``invalidate()`` landed while that computation was in flight. Caching it anyway would serve a
    stale answer - computed from data a concurrent write already superseded - for the rest of the
    TTL (Copilot review, PR #348); the caller still returns its own freshly-computed ``results``
    to whoever asked for them, this only decides whether the *cache* keeps a copy.
    """
    with _lock:
        if generation != _generation:
            return
        _store[key] = (time.monotonic(), results)
        _store.move_to_end(key)
        while len(_store) > _MAX_ENTRIES:
            _store.popitem(last=False)


def invalidate() -> None:
    """Drop every cached ranking result and advance the generation counter. Called after a
    phenology rebuild cutover (``scoring.regions._build_phenology_locked``) and after any
    trails/campsites/public-land/fire-perimeter write (``cache._invalidate_rank_cache``) - the
    things that can change what these keys resolve to sooner than their TTL."""
    global _generation
    with _lock:
        _store.clear()
        _generation += 1


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
    """Cache key for ``rank_destinations``. Exact coordinates/radius, not rounded (Copilot
    review, PR #348: rounding for the key while ``_rank_candidates``'s ``keep()`` and
    ``distance_km`` use the exact request values could serve a region right at the radius
    boundary - or a wrong displayed distance - from a *different* nearby request's cached entry).
    The real-world hit rate this cache actually wants comes from a device's stored home value
    reread identically across a session, not from continuously-jittering coordinates, so losing
    the near-miss rounding doesn't cost much."""
    return (
        "radial",
        months,
        taxon_ids,
        home_lat,
        home_lng,
        radius_km,
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
    """Cache key for ``rank_destinations_corridor``. Same exact-values rationale as ``radial_key``."""
    return (
        "corridor",
        months,
        taxon_ids,
        start_lat,
        start_lng,
        dest_lat,
        dest_lng,
        corridor_km,
        cell_deg,
        recent_weeks,
    )
