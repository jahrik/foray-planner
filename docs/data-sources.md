# Data sources

All scored/ingested data is fetched at ingest time and cached in Postgres. The app runs queries
against the cache - no live network calls happen during normal use. The satellite overlay is the
one exception worth calling out: it's basemap imagery, not scored data, so it's cached per-region
on first request rather than ingested up front for the whole coverage area - see Esri World
Imagery below.

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

**Role:** Official developed campground data - names, locations, fees, reservability.

- **Key:** Free API key from [ridb.recreation.gov](https://ridb.recreation.gov/landing).
  Set as `RIDB_API_KEY` in your environment or `.env` file. If unset, camps ingest is a
  silent no-op and everything else still works.
- **Per-state listing:** The primary path pages `facilities?state=XX&activity=CAMPING&full=true`
  for every state the home disk reaches (from `FORAY_COVERAGE` bboxes), then clips each
  facility to the true radius with the haversine formula. RIDB's point+radius search silently
  returns only ~1/3 of the developed campgrounds actually present (it matches on the facility's
  own coordinate, often unset), so it's kept only as a fallback for a non-US home. `full=true`
  gives `Reservable` on each record.
- **Fees:** RIDB ships fees as prose ("Camping: $16/vehicle... $2 per extra vehicle"), parsed
  best-effort into `fee_low`/`fee_high` - amounts qualified as add-ons / discounts / non-camping
  (extra vehicle, day use, senior, deposit) are dropped.
- **`free` flag:** Only set `TRUE` on an explicit no-fee signal *and* no positive fee amount
  anywhere in the blob. Never guessed from missing data.
- **Pruning:** Camp ingests only upsert, so a shrunk radius / moved home would strand old rows;
  `prune_campsites_outside_radius` clears `source='ridb'` rows outside the current disk after
  every ingest.
- **Terms:** Government data, free for use with attribution.

---

## OpenStreetMap / Overpass API

**Role:** Reported dispersed-camping sites, and the trail network (paths, named hiking routes,
trailheads).

- **Client:** httpx (no key required)
- **Endpoint:** [Overpass API](https://overpass-api.de) - `https://overpass-api.de/api/interpreter`
- **Rate limit:** Polite: sleep between requests; 429 responses respect the `Retry-After` header
- **Dispersed camping (`sources/dispersed.py`):**
  - `tourism=camp_site`, `tourism=camp_pitch`, `backcountry=yes` → `kind='reported'` campsites
- **Trails (`sources/trails.py`):**
  - `highway=path` ways → `kind='path'` (we exclude `highway=footway` - ~6x the rows, mostly
    urban sidewalks, and heavy enough to time the query out)
  - `route=hiking` relations → `kind='route'`, member ways stitched into a MultiLineString
  - `highway=trailhead` nodes → `kind='trailhead'` (a `Point`)
  - **Query quirk:** inside a `(...)` union, Overpass's `out geom` returns a relation with only
    `bounds` and no members, so the route clause gets its own `out geom;` after the way/node
    union - without it, `kind='route'` rows silently never appear.
  - **Preload link:** at ingest, each trailhead node is snapped (≤35 m, point-to-segment) onto
    the trail polylines in the payload and the matched trail ids are stored in `trails.connects`
    - expanded to every way sharing that trail's name. Selecting a trailhead then draws the
    whole named trail straight from cache; a live per-selection Overpass query is only the
    fallback for an unlinked trailhead, and its result is written back to `connects`.
  - **`length_km` / `attrs`:** great-circle length of the full polyline, and the kept OSM detail
    tags (`surface`, `sac_scale`, `trail_visibility`, `network`, `operator`, `informal`).
  - Informational only - links the OSM element page, makes no legal-access claim.
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

## Esri World Imagery + labels (satellite overlay)

**Role:** An opt-in layer (the Layers pill's "Aerial imagery" toggle, issue #301 - it was
auto-on-select before). When on, selecting a destination fills its true footprint with a
satellite image plus a matching roads/labels overlay (`showSatelliteOverlay`,
`frontend/src/map/map.ts`) so the ground under the focused circle reads sharp and bold against
the rest of the map, without losing the road/city names the basemap would otherwise show there.

- **Server-side, cached forever per region** (`region_satellite` table, `sources/satellite.py`).
  The frontend's two `<img>` tags request `/api/destinations/{region_id}/satellite/{image,labels}`
  instead of Esri directly; that route serves the cached bytes (browser-cacheable forever -
  `Cache-Control: immutable`) or, on a genuine cache miss, fetches + caches on the spot (a
  per-region lock coalesces the two `<img>` tags' near-simultaneous requests so a cold region
  only pays the fetch once). `foray backfill-satellite` pre-fetches every region in the `regions`
  table ahead of time so this cold path is rare in practice.
- **Endpoints:** both services are real XYZ tile pyramids under `server.arcgisonline.com`,
  confirmed live via their `MapServer?f=json` capabilities (`"Map,Tilemap"`, not the dynamic
  `MapServer/export` renderer `sources/land`/`sources/fire` use) - called only from
  `sources/satellite.py` (never the browser):
  - `World_Imagery/MapServer/tile/{z}/{y}/{x}` - the aerial photo (jpg). No labels baked in.
  - `Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}` - a transparent PNG of
    roads/borders/place labels, Esri's standard pairing for `World_Imagery` (the "hybrid"
    satellite view), drawn on top.
  `fetch_region_satellite` picks a zoom level for the region's radius, fetches every tile
  covering its true (Web-Mercator-circle) bounding box, and stitches + crops them into one raster
  per layer with Pillow - both layers fetched concurrently to halve cold-cache latency.
- **Why tiles, not `/export`:** a `v1` of this fetched one big image from `MapServer/export`
  instead. Two problems killed that approach: it renders on demand (25-45s per call, and degrades
  to a ~95% failure rate under just 6 concurrent requests - Esri's free export service does not
  hold up under sustained load), and its label layer draws text at a *fixed pixel height*
  regardless of the requested resolution or `dpi` (confirmed live - this service also reports
  `supportsDynamicLayers: false`, so there's no server-side override). That meant labels were
  either legible-but-blurry (stretched onto the sharp imagery's bounds) or crisp-but-illegible
  (rendered at the imagery's own high resolution, where the fixed-height text becomes a hairline).
  A real tile pyramid doesn't have either problem: each zoom level's tiles are pre-rendered and
  CDN-served (near-instant, no live-render latency or concurrency fragility) with text already
  sized correctly for that zoom - the same reason the OSM basemap tiles never have this issue.
- **Resolution:** fetched once per region, at a zoom level chosen for ~2048px across the disk,
  and never re-requested on zoom - the bounds are fixed geo coordinates, so Leaflet re-scales the
  same raster for any on-screen zoom level for free.
- **No key required.** CORS-open (`Access-Control-Allow-Origin: *`) on both services, confirmed
  against the live endpoints.
- **CSP:** `img-src` doesn't need an Esri entry - the browser only ever talks to our own origin
  (`'self'`) for satellite imagery now.
- **Attribution:** "Imagery © Esri", added to the Leaflet attribution control only while a
  selection is active (`map.attributionControl.addAttribution`/`removeAttribution`).
- **Backfill:** `foray backfill-satellite [--limit N] [--concurrency N]` (default concurrency 8) -
  fetches every region in `regions` missing from `region_satellite`. Safe to re-run (only
  fetches what's still missing). Tile fetches are cheap and reliable, so this finishes in minutes
  even at national scale - no need to run it in the background across hours the way a live-render
  approach would.

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
