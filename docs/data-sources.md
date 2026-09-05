# Data sources

All scored/ingested data is fetched at ingest time and cached in Postgres. The app runs queries
against the cache - no live network calls happen during normal use, with one exception: the
satellite overlay is basemap imagery, not scored data, fetched client-side the same way the OSM
tile basemap already is (see Esri World Imagery below).

---

## iNaturalist

**Role:** The core data source - research-grade fungal observations that drive all phenology
scoring.

- **Client:** [pyinaturalist](https://pyinaturalist.readthedocs.io/) (unofficial Python wrapper)
- **Throttle:** ~1 request/second; descriptive `User-Agent` header sent on every request
- **Pagination:** Deep-paginated via `id_above` to avoid the 10,000-result API cap
- **Retries:** `_with_retries` in `sources/inat.py` backs off on transient network errors
- **Filter:** `quality_grade=research` only - verifier-confirmed observations with mapped
  coordinates. `needs_id` and `casual` are excluded from all scoring.
- **Terms:** [iNaturalist API reference](https://www.inaturalist.org/pages/api+reference) -
  respect rate limits, send a descriptive User-Agent, no bulk scraping
- **License:** Observations are CC-BY-NC; cached locally for private trip planning, not
  redistribution or public serving of raw observation records

---

## Recreation.gov RIDB API

**Role:** Official developed campground data - names, locations, fees.

- **Key:** Free API key from [ridb.recreation.gov](https://ridb.recreation.gov/landing).
  Set as `RIDB_API_KEY` in your environment or `.env` file. If unset, camps ingest is a
  silent no-op and everything else still works.
- **Tiling:** The home radius is tiled into ≤50-mile query circles to work within the API's
  per-query radius limit. Facilities are deduped by ID and clipped to the true radius with
  the haversine formula.
- **`free` flag:** Only set `TRUE` on an explicit no-fee signal from the API response.
  Never guessed from missing data.
- **Terms:** Government data, free for use with attribution.

---

## OpenStreetMap / Overpass API

**Role:** Reported dispersed-camping sites.

- **Client:** httpx (no key required)
- **Endpoint:** [Overpass API](https://overpass-api.de) - `https://overpass-api.de/api/interpreter`
- **Rate limit:** Polite: sleep between requests; 429 responses respect the `Retry-After` header
- **What we fetch:**
  - `tourism=camp_site`, `tourism=camp_pitch`, `backcountry=yes` → `kind='reported'` campsites
- **License:** [ODbL](https://opendatacommons.org/licenses/odbl/) - data must be attributed
  and any derivative databases shared under ODbL
- **Attribution required:** "© OpenStreetMap contributors" in any UI showing this data
  (already in the Leaflet tile attribution)

### Off-limits sources

| Source | Reason |
|---|---|
| **iOverlander** | [ToS](https://ioverlander.com/terms_2023) is personal/non-commercial use only - no redistribution or caching. Incompatible with serving from a backend. |
| **The Dyrt** | Proprietary, no open API. |

OSM already carries real tagged campsites (`tourism=camp_site`), so we get the "reported
spots" value without the license problem. Do not add iOverlander or The Dyrt.

---

## ArcGIS / BLM + USFS land boundaries

**Role:** Public-land ownership polygons - shows what agency manages the land near a hotspot.

- **Sources:**
  - BLM Surface Management Agency (SMA) layer - filtered to `ADMIN_AGENCY_CODE='BLM'`
  - USFS Admin Forest boundaries
- **API:** ArcGIS REST FeatureServer `query?f=geojson` - paginated, server-side generalized
  (reduces geometry complexity before transfer)
- **No key required**
- **Storage:** GeoJSON stored as text + bounding-box columns. No PostGIS geometry types needed -
  bbox overlap in SQL is sufficient for the "land near here" query.
- **Attribution:** BLM and USFS are US federal agencies; data is public domain.
- **PAD-US** (USGS national ownership layer) is a documented backstop if the ArcGIS sources
  change or go offline.

> **Important:** Land polygons are informational only. They show who manages the land; they
> never assert camping legality. The UI labels them as ownership data and links the official
> source. Keep it that way.

---

## NIFC / MTBS wildfire ArcGIS services

**Role:** Active wildfire perimeters + points (safety/access) and recent burn scars
(burn-morel opportunity), issue #227. Cloned from the BLM/USFS ArcGIS pattern.

- **Client:** httpx (no key required)
- **Endpoints (public ArcGIS feature services):**
  - Active perimeters: NIFC **WFIGS Current Interagency Fire Perimeters**
  - Active points: NIFC **WFIGS Current Interagency Fire Locations** (small/new fires, no perimeter yet)
  - History: NIFC **InterAgency Fire Perimeter History** (windowed to the last 3 completed fire
    years + current, matching the burn-morel productivity curve)
  - Severity: **MTBS Burned Area Boundaries** (`dominant_severity` join, published ~1.5-2 yr
    after a season - recent scars stay `NULL` and the layer still works)
- **Storage:** GeoJSON text + bbox + representative center in `fire_perimeters`. No PostGIS.
- **Refresh lanes:** `wfigs_active` uses replace semantics (a contained fire is deleted, not
  kept); `perimeter_history` is a plain upsert.
- **Terms:** US government open data, free to use.
- **No claims:** popups link the official incident page (InciWeb / NIFC); the app never asserts
  a road or forest closure - same posture as land ownership.
- **Tests:** Network-mocked with `httpx.MockTransport`.

---

## Esri World Imagery (satellite overlay)

**Role:** Fills a selected destination's true footprint with a satellite image
(`showSatelliteOverlay`, `frontend/src/map/map.ts`) so the ground under the focused circle reads
sharp and bold against the rest of the map.

- **Client:** Browser-side, direct `fetch`/`<img>` request - not ingested, not cached in
  Postgres, no backend route. Basemap imagery, not scored/ingested data, so it doesn't follow
  the "cached in Postgres, no live network calls" rule the rest of this doc describes - the same
  exception the OSM tile basemap itself already is.
- **Endpoint:** ArcGIS REST `MapServer/export` (`server.arcgisonline.com/.../World_Imagery`) -
  one raster image per selection sized to that circle's bounding box, not a tile pyramid.
- **No key required.** CORS-open (`Access-Control-Allow-Origin: *`), confirmed against the live
  endpoint.
- **CSP:** allow-listed in `src/foray/api/security.py`'s `img-src` alongside the OSM tile hosts.
- **Attribution:** "Imagery © Esri", added to the Leaflet attribution control only while a
  selection is active (`map.attributionControl.addAttribution`/`removeAttribution`).
- **Rate limits:** Esri doesn't publish a hard free-tier cap for this public service; usage here
  is one request per user selection (not bulk/background), which keeps volume low. If this ever
  needs to scale up, revisit before relying on it more heavily.

---

## OpenStreetMap Nominatim (geocoding)

**Role:** Resolves place-name strings typed in the location bar to lat/lng coordinates.

- **Endpoint:** `https://nominatim.openstreetmap.org`
- **Policy:** [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/)
  - max 1 request/second, descriptive `User-Agent` required, no bulk geocoding
- **Attribution:** "© OpenStreetMap contributors"
- **Fallback:** Raw `lat,lng` input bypasses geocoding entirely (parsed directly in `sources/geocode.py`)
- **Tests:** Network-mocked with `httpx.MockTransport` - geocoding tests never hit the real API

---

## Open-Meteo (elevation + precipitation)

**Role:** Ground elevation per observation (issue #36) and rainfall (issue #226) - antecedent
rain per observation plus recent rain per destination. Informational readouts only; no scoring
or filtering, same posture as land ownership.

- **Client:** httpx (no key required)
- **Endpoints:**
  - Elevation: `https://api.open-meteo.com/v1/elevation` (Copernicus GLO-90 DEM)
  - Rain history: `https://archive-api.open-meteo.com/v1/archive` (`daily=precipitation_sum`,
    ERA5). ERA5 runs ~5-7 days behind and returns `null` for a day it has no value for yet -
    a window containing a null day is left `NULL` and retried, never summed partially.
  - Recent rain: `https://api.open-meteo.com/v1/forecast` (`past_days=30&daily=precipitation_sum`)
- **Rate limit:** 600 requests/min free tier (plus hourly/daily caps). A process-wide throttle
  (`sources/http.Throttle`) paces requests; 429s are honoured via `Retry-After` and the run
  resumes on the next scheduled pass.
- **Grid snapping:** precipitation lookups snap to the same `{ilat}_{ilng}` region grid the
  phenology tables use (`foray.geo.grid_cell`) and cache raw daily values in `precip_daily`, so
  many observations and the per-destination layer share one series per cell - "reuse the grid,
  don't invent a second geography."
- **Terms:** Free for non-commercial use; [Open-Meteo terms](https://open-meteo.com/en/terms).
  "© Open-Meteo" is shown in the map credits line (`map.ts` `TILE_ATTRIBUTION`).
- **Tests:** Network-mocked with `httpx.MockTransport`; the suite never hits the real API.
