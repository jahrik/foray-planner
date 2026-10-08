"""External data-source clients and the ingest layer built on them (issue #242).

Everything that talks to a third-party API and lands the result in the cache lives here:

* ``http`` / ``overpass`` - shared HTTP + Overpass plumbing (retry, throttle, User-Agent)
* ``inat`` - iNaturalist observations, the fungi-genus catalog and photos
* ``ingest`` - the observation ingest (``ingest`` / ``ingest_region``), the re-check passes
  (``revalidate`` / ``resync``) and the elevation / rain enrichment
* ``geocode`` - Nominatim place search / reverse geocode
* ``elevation`` / ``elevation_dem`` / ``precip`` - Open-Meteo lookups and local Copernicus DEM tiles
* ``camps`` / ``dispersed`` / ``land`` / ``trails`` - per-area source fetch + ``ingest_*``
* ``fire`` / ``ravg`` - NIFC wildfire perimeters and burn severity
* ``satellite`` - Esri imagery stitched from the tile pyramid
* ``inat_bulk`` / ``usfs_trails`` / ``usfs_mvum`` / ``osm_trails`` / ``trails_snapshot`` - the
  bulk-snapshot stagers and loaders (see ``foray.ingest_bulk``)
* ``ingest_base`` - the shared area-ingest skeleton (``run_area_ingest``)

The modules listed in ``__all__`` below are re-exported here so ``from foray.sources import camps``
and ``foray.sources.camps.<fn>`` work after a bare ``import foray.sources``; import the others
(``fire``, ``satellite``, the bulk modules) by their full path.
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
