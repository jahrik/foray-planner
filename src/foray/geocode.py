"""Resolve a place name (or a raw ``lat,lng`` string) to coordinates.

Uses OpenStreetMap Nominatim for name lookups - free, no key, but capped at ~1 req/s and
requires a descriptive User-Agent. Location changes are occasional, so this stays polite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "foray-planner/0.1 (mushroom trip planner; +https://github.com/jahrik)"

# "43.37, -124.22" or "43.37 -124.22" - a raw coordinate pair.
_COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$")


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
    owns = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
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
