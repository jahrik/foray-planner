"""Resolve a place name (or a raw ``lat,lng`` string) to coordinates.

Uses OpenStreetMap Nominatim for name lookups - free, no key, but capped at ~1 req/s and
requires a descriptive User-Agent. Location changes are occasional, so this stays polite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from foray.http import USER_AGENT, Throttle

NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"

# "43.37, -124.22" or "43.37 -124.22" - a raw coordinate pair.
_COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$")

# Nominatim's usage policy caps requests at ~1/s. Every existing caller (location search,
# `set_location`'s reverse lookup) fires rarely enough that this never mattered - but issue
# #206's destination-card place titles can trigger one `notable_place_name()` call per uncached
# region on a single ranking refresh, all from concurrent FastAPI requests. A process-wide lock
# + minimum interval (foray.http.Throttle) serializes calls to either function (they share this
# throttle), so a burst of cold cards degrades to "one card populates per second" instead of
# hammering the API. Callers cache results (see
# cache.region_places) precisely so this only bites once per
# region, ever.
_throttle = Throttle(1.1)


# Priority order for picking the one "biggest thing here" label out of Nominatim's structured
# address breakdown (issue #206) - notable/large place types first (a national forest or park
# beats the city it happens to sit near), then progressively smaller settlement types, down to
# county as the last resort before giving up entirely.
_PLACE_PRIORITY = (
    "national_park",
    "protected_area",
    "forest",
    "state_forest",
    "nature_reserve",
    "city",
    "town",
    "village",
    "hamlet",
    "county",
)


@dataclass(frozen=True)
class Location:
    name: str
    lat: float
    lng: float


def resolve(query: str, *, client: httpx.Client | None = None) -> Location:
    """Resolve ``query`` to a Location. Accepts a raw ``lat,lng`` pair or a place name."""
    match = _COORDS.match(query)
    if match:
        lat, lng = float(match.group(1)), float(match.group(2))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError(f"coordinates out of range: {query!r}")
        return Location(name=f"{lat:.4f}, {lng:.4f}", lat=lat, lng=lng)
    return _geocode(query, client=client)


def reverse(lat: float, lng: float, *, client: httpx.Client | None = None) -> Location:
    """Reverse-geocode ``lat``/``lng`` to a human-readable place name.

    Same client/User-Agent/timeout pattern as ``resolve()``/``_geocode()``, hitting Nominatim's
    ``/reverse`` endpoint instead of ``/search`` (issue #145) - keeps the one Nominatim client
    (and its rate-limit/User-Agent policy) in one place instead of duplicated per-caller.
    """
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError(f"coordinates out of range: {lat},{lng}")
    owns = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        _throttle.wait()
        resp = client.get(
            NOMINATIM_REVERSE,
            params={"lat": lat, "lon": lng, "format": "json"},
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns:
            client.close()
    name = data.get("display_name")
    if not name:
        raise LookupError(f"no place name found for {lat},{lng}")
    return Location(name=name, lat=lat, lng=lng)


def notable_place_name(lat: float, lng: float, *, client: httpx.Client | None = None) -> str | None:
    """The one "biggest thing here" place name for ``lat``/``lng`` (issue #206), or ``None``
    when nothing in ``_PLACE_PRIORITY`` is present (a remote/rural point) - a destination card
    falls back to showing just rank + distance in that case, same as before this existed.

    Uses Nominatim's structured ``address`` breakdown (``addressdetails=1``) rather than the
    free-text ``display_name`` used by ``reverse()`` - picking a specific field lets a national
    forest outrank the city it happens to be reverse-geocoded to, instead of always showing
    whatever the most zoomed-in address component is.
    """
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError(f"coordinates out of range: {lat},{lng}")
    owns = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        _throttle.wait()
        resp = client.get(
            NOMINATIM_REVERSE,
            params={"lat": lat, "lon": lng, "format": "json", "addressdetails": 1, "zoom": 14},
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns:
            client.close()
    address = data.get("address") or {}
    for field in _PLACE_PRIORITY:
        value = address.get(field)
        if value:
            return str(value)
    return None


def _geocode(query: str, *, client: httpx.Client | None = None) -> Location:
    owns = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        resp = client.get(
            NOMINATIM,
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns:
            client.close()
    if not data:
        raise LookupError(f"no location found for {query!r}")
    top = data[0]
    return Location(
        name=top.get("display_name", query),
        lat=float(top["lat"]),
        lng=float(top["lon"]),
    )
