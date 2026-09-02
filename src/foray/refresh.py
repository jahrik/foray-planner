"""The shared ingest-refresh orchestration.

The CLI (``foray refresh``) and the API (``POST /api/refresh``) warm the same home-radius
ingest sequence - observations, then camps/land/dispersed/trails, then a phenology rebuild.
``run_home_refresh`` is that one sequence; both callers drive it, differing only in what
they wrap around it:

- the CLI passes no HTTP client, no cancellation, no progress callback, and prints its own
  summary afterward. Its coverage-wide ``--all`` mode is a *different* sequence (per-region
  ``ingest_region`` / ``ingest_public_land_coverage`` / ``ingest_trails_region``) and stays
  in ``cli.py``.
- the API runs it in a background thread with a shared long-timeout ``httpx.Client``, a
  ``threading.Event`` for cancellation, and a progress callback that broadcasts SSE updates
  (see ``foray.api.refresh_runner``).

The target vocabulary and the month-list parsing live here too, so the two callers can't
drift apart on those either.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Collection

import httpx
import psycopg

from foray import scoring
from foray.config import Settings
from foray.sources import camps, dispersed, land, trails
from foray.sources.ingest import ingest

# The layers a refresh can warm, in ingest order. The API additionally accepts the
# pseudo-target ``"all"`` (every layer); the CLI expresses "everything" as an empty ``--with``.
REFRESH_LAYERS: tuple[str, ...] = ("mushrooms", "camps", "land", "dispersed", "trails")
REFRESH_TARGETS: frozenset[str] = frozenset({"all", *REFRESH_LAYERS})

# Relative weight of each phase on the API's 0-100 progress bar. Phenology only runs (and
# only claims its slice) when mushrooms is one of the refreshed layers. A refresh of a
# single layer fills the whole bar with that layer regardless of its weight here.
_PHASE_WEIGHTS: dict[str, float] = {
    "mushrooms": 50.0,
    "camps": 10.0,
    "land": 10.0,
    "dispersed": 10.0,
    "trails": 10.0,
    "phenology": 10.0,
}

# The four home-radius area ingests, in order, as (layer, module, function name). Resolved
# by name at call time so tests can monkeypatch the underlying ingest. ``ingest``
# (observations) is handled separately because it takes an ``abort_event`` the others don't.
_AREA_INGESTS: tuple[tuple[str, object, str], ...] = (
    ("camps", camps, "ingest_campgrounds"),
    ("land", land, "ingest_public_land"),
    ("dispersed", dispersed, "ingest_dispersed"),
    ("trails", trails, "ingest_trails"),
)

ProgressFn = Callable[[str, float], None]


def parse_month_list(months: str) -> list[int]:
    """Parse a comma-separated month list (e.g. ``"3,4,5"``) into ints, validating the range.

    Raises ``ValueError`` with a caller-friendly message on non-integer or out-of-range
    input. An empty/whitespace string yields ``[]`` so each caller can apply its own default
    (the current month for a point query, all twelve for a whole-year rollup).
    """
    try:
        values = [int(token) for token in months.split(",") if token.strip()]
    except ValueError:
        raise ValueError(f"months must be integers 1-12: {months!r}") from None
    if not all(1 <= month <= 12 for month in values):
        raise ValueError("months must be in 1-12")
    return values


def _progress_slices(layers: Collection[str]) -> dict[str, tuple[float, float]]:
    """Map each active phase to its ``(base_pct, span_pct)`` on a 0-100 progress bar, sized
    by :data:`_PHASE_WEIGHTS`. A ``"phenology"`` slice is appended when mushrooms is active.
    """
    phases = [layer for layer in REFRESH_LAYERS if layer in layers]
    if "mushrooms" in layers:
        phases.append("phenology")
    total = sum(_PHASE_WEIGHTS[phase] for phase in phases) or 1.0
    slices: dict[str, tuple[float, float]] = {}
    cursor = 0.0
    for phase in phases:
        span = _PHASE_WEIGHTS[phase] / total * 100.0
        slices[phase] = (cursor, span)
        cursor += span
    return slices


def run_home_refresh(
    cfg: Settings,
    conn: psycopg.Connection,
    layers: Collection[str],
    *,
    client: httpx.Client | None = None,
    abort_event: threading.Event | None = None,
    progress_cb: ProgressFn | None = None,
) -> None:
    """Warm the home-radius ingest for each requested layer, then rebuild phenology.

    ``layers`` is any subset of :data:`REFRESH_LAYERS`. ``client`` is shared across the area
    ingests when given (the API reuses one long-timeout connection); when ``None`` each
    ingest opens its own. When ``abort_event`` is set the run stops at the next phase
    boundary (and ``ingest`` itself checks it mid-stream). ``progress_cb(step, pct)`` fires
    with an overall 0-100 percentage; pass ``None`` to run silently (the CLI).
    """
    aborted = (lambda: abort_event is not None and abort_event.is_set()) if abort_event else (lambda: False)
    slices = _progress_slices(layers)

    def phase_cb(phase: str) -> ProgressFn | None:
        if progress_cb is None or phase not in slices:
            return None
        base, span = slices[phase]
        return lambda step, local_pct: progress_cb(step, base + span * (local_pct / 100.0))

    if "mushrooms" in layers and not aborted():
        ingest(cfg, conn, progress_cb=phase_cb("mushrooms"), abort_event=abort_event)

    for layer, module, func_name in _AREA_INGESTS:
        if layer in layers and not aborted():
            getattr(module, func_name)(cfg, conn, client=client, progress_cb=phase_cb(layer))

    if "mushrooms" in layers and not aborted():
        if progress_cb is not None:
            progress_cb("Building phenology…", slices["phenology"][0])
        scoring.build_phenology(conn, cfg.cell_deg)
