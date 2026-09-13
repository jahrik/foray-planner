"""Open-Meteo precipitation lookups (issue #226).

Two endpoints, one shape - a daily ``precipitation_sum`` series for a single point:

* :func:`fetch_archive_precip` - the **Archive API** (ERA5), for per-observation antecedent
  rainfall. ERA5 runs ~5-7 days behind real time and returns ``null`` for a day it has no
  value for yet; this module passes that through as ``None`` (never ``0.0``) so the caller can
  refuse to record a partial-window sum.
* :func:`fetch_recent_precip` - the **Forecast API** with ``past_days``, for the recent-rain
  per-destination layer (Part 2). :func:`fetch_recent_precip_batch` is the multi-point form
  (issue #334 PR 3) - every region's recent-rain refresh wants the *same* trailing window
  ending today, so up to ``MAX_BATCH`` region centers ride in one request instead of one each,
  the same win :mod:`foray.sources.elevation` gets from batching coordinates.

Free, no key - same provider as :mod:`foray.sources.elevation`. The archive antecedent-rainfall
path (:func:`fetch_archive_precip`) stays per-point: each observation's window is grouped by
grid cell already (``ingest.backfill_precip``), but different cells need different date ranges
(each cell's own oldest-pending-observation date), so there's no shared window to batch across
cells the way the recent-rain refresh has.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Mapping, Sequence

import httpx

from foray.sources.http import USER_AGENT, Throttle, retry_after_seconds

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

ARCHIVE_SOURCE = "open-meteo-archive"
FORECAST_SOURCE = "open-meteo-forecast"

# ~0.2 s between calls ≈ 300/min, comfortably under Open-Meteo's 600/min free ceiling; the
# hourly / daily caps surface as 429s, which _get_series rides out with Retry-After backoff
# before giving up so the next scheduled pass resumes. Process-wide (shared Throttle), so a
# burst of concurrent refreshes still paces to one call per interval.
_throttle = Throttle(0.2)

_MAX_RETRIES = 3
_MAX_RETRY_WAIT_S = 120.0


def _check_coords(lat: float, lng: float) -> None:
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError(f"coordinates out of range: {lat},{lng}")


def _get_series(url: str, params: dict[str, str | int], *, client: httpx.Client | None) -> dict[dt.date, float | None]:
    """GET a ``daily.precipitation_sum`` series and return it as ``date -> mm`` (``None`` for a
    day the API returned null). Raises ``httpx.HTTPError`` on a network/HTTP failure."""
    owns = client is None
    client = client or httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT})
    try:
        for attempt in range(_MAX_RETRIES + 1):
            _throttle.wait()
            resp = client.get(url, params=params)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                time.sleep(retry_after_seconds(resp, attempt, cap=_MAX_RETRY_WAIT_S))
                continue
            resp.raise_for_status()
            break
        daily = resp.json().get("daily") or {}
    finally:
        if owns:
            client.close()
    dates = daily.get("time") or []
    values = daily.get("precipitation_sum") or []
    series: dict[dt.date, float | None] = {}
    for iso, value in zip(dates, values, strict=False):
        series[dt.date.fromisoformat(iso)] = float(value) if value is not None else None
    return series


def fetch_archive_precip(
    lat: float, lng: float, start: dt.date, end: dt.date, *, client: httpx.Client | None = None
) -> dict[dt.date, float | None]:
    """Daily precipitation (mm) for ``[start, end]`` inclusive at ``(lat, lng)``, from ERA5.

    A day still inside ERA5's ~5-7 day lag comes back as ``None``; missing entirely from the
    result means the API returned no row for it at all. Either way the caller treats it as
    "not known yet"."""
    _check_coords(lat, lng)
    return _get_series(
        ARCHIVE_URL,
        {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lng:.4f}",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": "precipitation_sum",
            "timezone": "GMT",
        },
        client=client,
    )


def fetch_recent_precip(
    lat: float, lng: float, *, past_days: int = 30, client: httpx.Client | None = None
) -> dict[dt.date, float | None]:
    """Daily precipitation (mm) for the trailing ``past_days`` days (plus today) at ``(lat,
    lng)``, from the forecast API's reanalysis of recent days."""
    _check_coords(lat, lng)
    return _get_series(
        FORECAST_URL,
        {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lng:.4f}",
            "past_days": past_days,
            "forecast_days": 1,
            "daily": "precipitation_sum",
            "timezone": "GMT",
        },
        client=client,
    )


# Open-Meteo's forecast API accepts comma-separated latitude/longitude for multiple locations
# in one request, one date range shared by all of them (verified live 2026-09-13: a 2-location
# request returns a JSON array, one object per location, each with its own `daily` series, in
# request order). No documented per-request location cap like the elevation endpoint's 100;
# 50 is a conservative choice - a big enough win to matter, small enough that a batch never
# ends up carrying more series than can comfortably sit in memory or one response body.
MAX_BATCH = 50


def fetch_recent_precip_batch(
    centers: Sequence[tuple[float, float]], *, past_days: int = 30, client: httpx.Client | None = None
) -> list[dict[dt.date, float | None]]:
    """Batched form of :func:`fetch_recent_precip` - one forecast-API call for up to
    ``MAX_BATCH`` centers sharing the same trailing-``past_days`` window (issue #334 PR 3),
    instead of one call per center. Returns one series per input center, in the same order.

    Paced the same as a single-point call (one ``_throttle`` unit, not one per center) - the
    whole point of batching is fewer requests for the same rate-limit budget, so metering by
    request count here (not by point count, unlike `elevation.lookup_batch`) is what actually
    realizes that win.
    """
    if not centers:
        return []
    if len(centers) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} points per request, got {len(centers)}")
    for lat, lng in centers:
        _check_coords(lat, lng)
    params = {
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in centers),
        "longitude": ",".join(f"{lng:.4f}" for _, lng in centers),
        "past_days": past_days,
        "forecast_days": 1,
        "daily": "precipitation_sum",
        "timezone": "GMT",
    }
    owns = client is None
    client = client or httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT})
    try:
        for attempt in range(_MAX_RETRIES + 1):
            _throttle.wait()
            resp = client.get(FORECAST_URL, params=params)
            if resp.status_code == 429 and attempt < _MAX_RETRIES:
                time.sleep(retry_after_seconds(resp, attempt, cap=_MAX_RETRY_WAIT_S))
                continue
            resp.raise_for_status()
            break
        payload = resp.json()
    finally:
        if owns:
            client.close()
    results: list[dict[dt.date, float | None]] = []
    for entry in payload:
        daily = entry.get("daily") or {}
        dates = daily.get("time") or []
        values = daily.get("precipitation_sum") or []
        results.append(
            {
                dt.date.fromisoformat(iso): (float(value) if value is not None else None)
                for iso, value in zip(dates, values, strict=False)
            }
        )
    return results


def window_sum(series: Mapping[dt.date, float | None], end: dt.date, days: int) -> float | None:
    """Total precipitation over the ``days`` calendar days ending ``end`` (inclusive).

    ``None`` - never a partial - if any day in that span is absent from ``series`` or cached as
    ``None`` (an ERA5-lag gap). A partial sum reads as "it was dry" when it just isn't known
    yet, so the column stays ``NULL`` and the row is retried later."""
    total = 0.0
    for offset in range(days):
        value = series.get(end - dt.timedelta(days=offset))
        if value is None:
            return None
        total += value
    return round(total, 1)
