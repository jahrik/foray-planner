"""Resolve a place name (or a raw ``lat,lng`` string) to coordinates.

Uses OpenStreetMap Nominatim for name lookups — free, no key, but capped at ~1 req/s and
requires a descriptive User-Agent. Location changes are occasional, so this stays polite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "foray-planner/0.1 (mushroom trip planner; +https://github.com/jahrik)"

# "43.37, -124.22" or "43.37 -124.22" — a raw coordinate pair.
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
