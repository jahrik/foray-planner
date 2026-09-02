"""Open-Meteo precipitation lookups (issue #226).

Two endpoints, one shape - a daily ``precipitation_sum`` series for a single point:

* :func:`fetch_archive_precip` - the **Archive API** (ERA5), for per-observation antecedent
  rainfall. ERA5 runs ~5-7 days behind real time and returns ``null`` for a day it has no
  value for yet; this module passes that through as ``None`` (never ``0.0``) so the caller can
  refuse to record a partial-window sum.
* :func:`fetch_recent_precip` - the **Forecast API** with ``past_days``, for the recent-rain
  per-destination layer (Part 2).

Free, no key - same provider as :mod:`foray.sources.elevation`. Each call is a full series for
one point+range (no multi-point batching), so the throttle paces by request, not by point.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Mapping

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
