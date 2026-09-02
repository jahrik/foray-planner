"""External data-source clients and the ingest layer built on them (issue #242).

Everything that talks to a third-party API and lands the result in the cache lives here:

* ``http`` / ``overpass`` - shared HTTP + Overpass plumbing (retry, throttle, User-Agent)
* ``inat`` - iNaturalist observations + fungi genera
* ``geocode`` - Nominatim place search / reverse geocode
* ``elevation`` - Open-Meteo elevation lookups
* ``camps`` / ``dispersed`` / ``land`` / ``trails`` - per-area source fetch + ``ingest_*``
* ``ingest_base`` - the shared area-ingest skeleton (``run_area_ingest``)
* ``ingest`` - the iNaturalist observation ingest (``ingest`` / ``ingest_region`` / ...)

Modules are re-exported here so ``from foray.sources import camps`` and
``foray.sources.camps.<fn>`` work after a bare ``import foray.sources``.
"""

from __future__ import annotations

from foray.sources import (
    camps,
    dispersed,
    elevation,
    geocode,
    http,
    inat,
    ingest,
    ingest_base,
    land,
    overpass,
    trails,
)

__all__ = [
    "camps",
    "dispersed",
    "elevation",
    "geocode",
    "http",
    "inat",
    "ingest",
    "ingest_base",
    "land",
    "overpass",
    "trails",
]
