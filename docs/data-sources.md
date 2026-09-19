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
  per state. `foray refresh --with camps` (home-radius, on-demand) lists the states the home
  disk reaches and clips each facility to the true radius; `foray refresh --with camps --all`
  (the weekly cron) lists *every* `FORAY_COVERAGE` state and keeps all of them, so a destination
  anywhere in coverage has its campgrounds cached. One-shot per query version (`camps:coverage:v{N}`
  in `ingest_log`); bumping `_CAMPS_COVERAGE_VERSION` makes the next cron re-list every state.
  RIDB's point+radius search silently returns only ~1/3 of the developed campgrounds actually
  present (it matches on the facility's own coordinate, often unset), so it's kept only as a
  fallback for a non-US home. `full=true` gives `Reservable` on each record. **Superseded by
  the `ridb` bulk snapshot** once one has loaded (issue #334 PR 2, see the bulk-snapshot section
  below) - this live per-state/coverage-wide crawl then skips itself automatically.
- **Fees:** RIDB ships fees as prose ("Camping: $16/vehicle... $2 per extra vehicle"), parsed
  best-effort into `fee_low`/`fee_high` - amounts qualified as add-ons / discounts / non-camping
  (extra vehicle, day use, senior, deposit) are dropped.
- **`free` flag:** Only set `TRUE` on an explicit no-fee signal *and* no positive fee amount
  anywhere in the blob. Never guessed from missing data.
- **Pruning:** Camp ingests only upsert, so a shrunk radius / moved home / shrunk coverage would
  strand old rows. The home-radius path runs `prune_campsites_outside_radius` (disk) when no
  coverage is configured; the coverage-wide path runs `prune_campsites_outside_bounds` (the
  union `FORAY_COVERAGE` envelope) and owns pruning whenever coverage is set.
- **Terms:** Government data, free for use with attribution.

---

## OpenStreetMap / Overpass API

**Role:** Reported dispersed-camping sites, and the trail network (paths, forest roads, named
hiking routes, trailheads).

- **Client:** httpx (no key required)
- **Endpoints:** `overpass-api.de` (primary), then public mirrors `overpass.kumi.systems` and
  `overpass.private.coffee` on connection failure or an exhausted 429/504 retry. Override with
  `FORAY_OVERPASS_URLS` (comma-separated).
- **Rate limit:** Polite: sleep between requests; 429 responses respect the `Retry-After` header
- **Dispersed camping (`sources/dispersed.py`):**
  - `tourism=camp_site`, `tourism=camp_pitch`, `backcountry=yes` → `kind='reported'` campsites
  - `foray refresh --with dispersed` covers the home disk; `--with dispersed --all` (the weekly
    cron) loops per coverage region, same one-shot-per-region-per-version marker
    (`dispersed:place:{place_id}:v{N}`) and tile-retry semantics as the trail ingest - a single
    Overpass hiccup only costs a re-crawl of that one region next run, not the whole coverage.
    State Parks and other non-federal campgrounds RIDB doesn't carry come in through this path.
- **Trails (`sources/trails.py`):**
  - `highway=path` / `highway=bridleway` ways → `kind='path'` (we exclude `highway=footway` -
    ~6x the rows, mostly urban sidewalks, and heavy enough to time the query out)
  - `highway=track`, and `highway=service` + `service=forestry` → `kind='road'` - old logging /
    forest-service roads, a primary foraging surface, kept separately queryable from trails
  - `route=hiking` relations → `kind='route'`, member ways stitched into a MultiLineString
  - `highway=trailhead` nodes → `kind='trailhead'` (a `Point`)
  - **Query quirk:** inside a `(...)` union, Overpass's `out geom` returns a relation with only
    `bounds` and no members, so the route clause gets its own `out geom;` after the way/node
    union - without it, `kind='route'` rows silently never appear.
  - **Preload link:** at ingest, each trailhead node is snapped (≤35 m, point-to-segment) onto
    the trail polylines in the payload and the matched trail ids are stored in `trails.connects`
    - expanded to every way sharing that feature's `name`, or its `ref`+`operator` for an
    unnamed forest road (OSM splits `FR 300` into dozens of ref-only segments). Selecting a
    trailhead then draws the whole named trail / whole numbered road straight from cache; a live
    per-selection Overpass query is only the fallback for an unlinked trailhead, and its result
    is written back to `connects`.
  - **`length_km` / `attrs`:** great-circle length of the full polyline, and the kept OSM detail
    tags (`highway`, `surface`, `tracktype`, `smoothness`, `4wd_only`, `sac_scale`,
    `trail_visibility`, `network`, `operator`, `informal`, `access`, `motor_vehicle`, `foot`,
    `ref`, and the `seasonal` / `access:conditional` / `motor_vehicle:conditional` /
    `foot:conditional` keys that flag a snow gate or winter closure - surfaced as a "seasonal"
    hint, never parsed into an open/closed decision).
  - **Re-ingest:** the per-region one-shot marker is `trails:place:{id}:q{_TRAILS_QUERY_VERSION}`.
    Widening the Overpass query bumps that constant, so the weekly `refresh --with trails --all`
    cron re-pulls every region automatically (superseded markers pruned on success) - no manual
    step. `foray trails --all --force` is a manual re-pull without a bump (OSM data drift).
  - **Road ranking:** `trails_near(sort="relevance")` re-weights `kind='road'` rows so
    target-genus obs-density along the line dominates (length is log-damped), and a road gated
    to motor vehicles but open on foot (`Trail.walk_in`) gets a bonus - walk-in ground is less
    picked.
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
  - Census TIGERweb **AIANNHA / Federal American Indian Reservations** (layer 2) - sovereign
    nation land, stored with `agency='Tribal'`
- **API:** ArcGIS REST FeatureServer `query?f=geojson` - paginated, server-side generalized
  (reduces geometry complexity before transfer)
- **No key required**
- **Storage:** parsed into a PostGIS `geom geography` column on `public_land` with a GiST index
  (`ix_public_land_geom`, issue #268); the "land near here" query is an index-backed `ST_DWithin`.
  The persisted bounding-box columns were dropped in the same change.
- **Attribution:** BLM, USFS and the Census Bureau are US federal agencies; data is public domain.
- **PAD-US** (USGS national ownership layer) is a documented backstop if the ArcGIS sources
  change or go offline.

> **Important:** Land polygons are informational only. They show who manages the land; they
> never assert camping legality. The UI labels them as ownership data and links the official
> source. Keep it that way.

---

## NIFC wildfire ArcGIS services + RAVG burn severity

**Role:** Active wildfire perimeters + points (safety/access) and recent burn scars
(burn-morel opportunity), issue #227. Cloned from the BLM/USFS ArcGIS pattern.

- **Client:** httpx (no key required)
- **Endpoints (public ArcGIS feature services):**
  - Active perimeters: NIFC **WFIGS Current Interagency Fire Perimeters**
  - Active points: NIFC **WFIGS Current Interagency Fire Locations** (small/new fires, no perimeter yet)
  - History: NIFC **InterAgency Fire Perimeter History** (windowed to the last 3 completed fire
    years + current, matching the burn-morel productivity curve)
- **Storage:** `fire_perimeters`, with a PostGIS `geom geography` column + GiST index
  (`ix_fire_perimeters_geom`, issue #268) backing the "fire near here" query and a representative
  center for the card. The GeoJSON text is kept for serving the map layer; the bbox columns are gone.
- **Refresh lanes:** `wfigs_active` uses replace semantics (a contained fire is deleted, not
  kept); `perimeter_history` is a plain upsert.
- **Terms:** US government open data, free to use.
- **No claims:** popups link the official incident page (InciWeb / NIFC); the app never asserts
  a road or forest closure - same posture as land ownership.
- **Tests:** Network-mocked with `httpx.MockTransport`.

**Burn severity (`dominant_severity`), issue #335 PR 4:** originally a live MTBS severity fetch
inside `refresh_fire` - that endpoint (`portal.mtbs.gov`) no longer resolves at all (confirmed
2026-09-14), so it had been failing silently on every refresh since #227 shipped. MTBS's own
live-confirmed national layer (`EDW_MTBS_01/MapServer/63` on `apps.fs.usda.gov`) doesn't carry
per-severity-class acreage, only boundary/metadata - so it isn't a drop-in fix. Severity now comes
from **RAVG** (`EDW_RAVG_v2_01/MapServer/0`, found via an ArcGIS Online org search after every
guessed name under `apps.fs.usda.gov/.../RDW_Wildfire` 403'd), a vegetation-mortality proxy
(`tree_acres`/`tree_ac_50`/`tree_ac_75`) mapped onto the same low/moderate/high shape
`cache.apply_fire_severity` expects. No shared id field exists between RAVG and our WFIGS-derived
rows, so matching falls back to a normalized name+year join (`cache._MTBS_UPDATE`'s `"name_year"`
key) - best-effort. Loaded via `ingest-bulk ravg` (`foray/sources/ravg.py`), per #357's bulk-source
standard - not a live per-refresh fetch. See that module's docstring for the full writeup.

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
- **Where the bytes live (issue #334 PR 1):** when `FORAY_SPACES__ACCESS_KEY_ID`/
  `SECRET_ACCESS_KEY`/`BUCKET` are set (`Settings.spaces`, `foray/spaces.py`), a save uploads
  both rasters to the DO Space instead and `region_satellite` keeps only their public URLs
  (`image_url`/`labels_url`) - the API route still serves the bytes itself (fetched from the
  Space over HTTP), not a redirect, so the response headers/caching behavior above are unchanged.
  Unconfigured (the default, and every dev box without a Spaces key) falls back to storing the
  bytes directly in `region_satellite`'s `image`/`labels` bytea columns, exactly as before -
  this is opt-in, not a breaking change. `FORAY_SPACES__*` also backs the bulk-snapshot staging
  path (`foray.ingest_bulk`, `bulk/{source}/{date}/`) - one Space, two prefixes.
- **Endpoints:** three real XYZ tile pyramids under `server.arcgisonline.com`, confirmed live via
  their `MapServer?f=json` capabilities (`"Map,Tilemap"`, not the dynamic `MapServer/export`
  renderer `sources/land`/`sources/fire` use) - called only from `sources/satellite.py` (never
  the browser), composited bottom-to-top:
  - `World_Imagery/MapServer/tile/{z}/{y}/{x}` - the aerial photo (jpg). No labels baked in.
  - `Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}` - a transparent PNG of road /
    trail lines, drawn over the imagery.
  - `Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}` - a transparent PNG of
    borders + place labels, Esri's standard pairing for `World_Imagery` (the "hybrid" satellite
    view), drawn on top.
  `fetch_region_satellite` picks a zoom level for the region's radius, fetches every tile
  covering its true (Web-Mercator-circle) bounding box, and stitches + crops them into one raster
  per layer with Pillow - all three layers fetched concurrently to halve cold-cache latency.
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

## Protomaps PMTiles vector basemap

**Role:** The map's only base layer. A self-hosted Protomaps PMTiles archive rendered by MapLibre
GL, mounted inside the existing Leaflet map via `@maplibre/maplibre-gl-leaflet`. Not scored -
it's cartography. This replaced the old raster basemap (Esri "World Light Gray Canvas" in light
mode, `tile.openstreetmap.org` CSS-inverted in dark) outright: there is no raster fallback, so
`FORAY_BASEMAP_URL` must be set or the map renders overlays with no base underneath.

**Why vector:** the raster base baked every road and label into pixels we could only draw over. A
vector base lets us style forest roads / trails / highways distinctly, declutter labels, drop the
`maxZoom: 14` ceiling, and get a real dark style instead of a CSS `invert()` hack.

- **Archive:** one PMTiles file (a single-file archive of Mapbox Vector Tiles, addressed by
  z/x/y over HTTP range requests) covering CONUS. ~15-40 GB - too big for the droplet root
  disk, so it lives in a DigitalOcean Space fronted by the Spaces CDN as one static object.
- **Build (Ansible):**
  - `just ansible provision` (`tasks/provision/basemap.yml`) creates the Space
    (`digitalocean.cloud.space`) + CDN endpoint (`digitalocean.cloud.cdn_endpoints`), a
    public-read bucket policy (`amazon.aws.s3_bucket`), and a CORS rule for ranged GETs
    (`community.aws.s3_cors`) - the S3 modules pointed at the Spaces `endpoint_url`. Skipped
    when `DO_SPACES_KEY` / `DO_SPACES_SECRET` (a Spaces access key, separate from
    `DO_API_TOKEN`) are unset.
  - `just ansible build-basemap-once` (`tasks/provision/build_basemap_once.yml`) fetches the
    `pmtiles` CLI, `pmtiles extract`s `foray_basemap_bbox` from the most recent
    `build.protomaps.com/<date>.pmtiles` (range requests, not a full planet download), uploads
    it with `amazon.aws.s3_object`, and purges the CDN key. Runs on the control node, not the
    droplet. Re-run monthly to refresh - the object key is stable.
- **Config:** `FORAY_BASEMAP_URL` if set, else the computed CDN URL once the Spaces key is
  configured, else empty (no base layer). Surfaced to the SPA in `GET /api/config` as
  `basemap_url`; the ansible var is `foray_basemap_url`.
- **Local dev:** point `FORAY_BASEMAP_URL` at the same deployed CDN archive
  (`https://<space>.<region>.cdn.digitaloceanspaces.com/us.pmtiles`). `http://localhost:8000`
  (the docker site) and `http://localhost:5173` (vite) are both in `foray_basemap_allowed_origins`,
  so the browser's ranged `fetch()` is allowed. The archive is public-read open map data, so
  those localhost CORS entries expose nothing a `curl` couldn't already get. An offline /
  smaller `pmtiles extract` served same-origin from `frontend/public/` is the alternative (see
  `.env.example`).
- **Frontend:** `frontend/src/map/basemap.ts`, code-split (the MapLibre GL stack is ~280 kB
  gzip) so it loads in parallel with first paint. Registers the `pmtiles://` protocol, builds a
  MapLibre style from `protomaps-themes-base` (light/dark), swaps the style on theme change.
  `maplibre-gl` is pinned to v6 with the Vite worker set via `setWorkerUrl(...?worker&url)` -
  see the comment in `basemap.ts` (a bare v6 bump 404s the worker silently).
- **Contrast pass:** `frontend/src/map/basemap-theme.ts` lifts the stock `dark` theme out of
  its ~15% luminance band (water was barely off the background, forest near-black, roads a hair
  above the land) - navy water, real dark greens, a legible road hierarchy and labels. `light`
  is left as the stock theme.
- **Road styling:** `frontend/src/map/basemap-roads.ts` splits the base theme's single dim
  `roads_other` layer, using the OSM `highway=` value Protomaps keeps in `kind_detail`, into a
  cased ochre "forest roads" layer (`highway=track`) and a cased green "trails" layer
  (footway/path/bridleway/steps/cycleway), each with its own line-following label. Protomaps'
  tiles carry no track/path geometry below ~z13, so these are a zoom-in feature.
- **Glyphs + sprites:** from `https://protomaps.github.io/basemaps-assets` (a few MB of
  font/icon data, not the tiles). Self-hosting these alongside the archive is a later step.
- **CSP:** `_content_security_policy(basemap_url)` in `api/security.py` adds `worker-src 'self'
  blob:` (MapLibre workers), `blob:` to `img-src`, `https://protomaps.github.io` to
  `connect-src` / `img-src`, and the configured basemap host to `connect-src`.
- **Attribution:** "© OpenStreetMap · © Protomaps", added to the Leaflet attribution control on
  mount.

---

## Terrain (DEM hillshade + contours)

**Role:** Relief shading + contour lines under the vector basemap, so a forager can read the
landform - aspect (north-facing slopes hold moisture longer), drainages, creek bottoms, benches.
Not scored - cartography.

**Source:** [AWS Open Data `elevation-tiles-prod`](https://registry.opendata.aws/terrain-tiles/) -
Terrarium-encoded raster DEM tiles (RGB channels encode ground elevation), the Tilezen/Joerd
composite: USGS 3DEP ~10 m over the continental US, SRTM 30 m elsewhere, served to ~z15. Free,
no key, CORS-open. `https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png`.

**Why not self-hosted:** unlike the vector basemap, a raster DEM pyramid does not compress and
there is no `pmtiles extract` shortcut for it, so self-hosting a CONUS archive is far more infra
(a build toolchain, a second Space, a ~20 GB working set) than the vector basemap was - for a
*lower*-resolution result if built from the ~90 m Copernicus GLO-90 tiles we already cache for
elevation. The AWS tiles give sharper relief with no infra. Switching back later is a one-line
URL change (`FORAY_TERRAIN_URL`).

- **Config:** `foray_terrain_url` / `FORAY_TERRAIN_URL` - the tile URL template. Defaults to the
  AWS set; override with a self-hosted URL. Unset or empty falls back to the default in a
  deploy; an empty `FORAY_TERRAIN_URL` in a local `.env` drops the layer. Surfaced to the SPA in
  `GET /api/config` as `terrain_url`.
- **Frontend (next PR):** a `raster-dem` source (`encoding: "terrarium"`, `maxzoom: 15`) + a
  `hillshade` layer spliced under the landcover fills (on by default) and `maplibre-contour`-
  derived contour lines + metre labels (behind a Layers-pill "Contours" checkbox, off by
  default). 2D hillshade only - no MapLibre 3D terrain, which fights the Leaflet 2D pane sync.
- **CSP:** `_content_security_policy(basemap_url, terrain_url)` adds the terrain tile host to
  `connect-src` (MapLibre + `maplibre-contour` fetch the tiles) and `img-src` (raster-dem tile
  decode); the contour worker is covered by the same `worker-src 'self' blob:`.
- **Attribution:** per the terrain-tiles dataset - USGS / NASA (SRTM) / NASADEM etc. via the
  Tilezen attribution string, added to the Leaflet attribution control.

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

---

## Bulk-snapshot ingest pipeline (issue #334)

**Role:** Machinery for authoritative bulk data sources too large or too infrequently updated
to fetch live per-region (issue #335: PAD-US, USFS Trail_NFS + MVUM, MTBS/RAVG) - not a source
itself.

**Which pipeline for a new source? (issue #357)** Ask two questions before writing a stager:
is it a *national/authoritative* dataset (a government shapefile/GeoPackage/CSV export covering
the whole country, not scoped to a query envelope), and is it *infrequently updated* (daily/
weekly, not something a user action needs fresh right now)? Both yes -> `ingest_bulk`: register
a `Stager`/`Loader` pair (`sources/usfs_trails.py` is the reference shape), and it's
automatically staged by `bulk-load.yml`'s `foray stage-snapshot --all` default and loaded by a
`jobs.yaml` entry - no separate registry to remember to update. Either answer is no -> a live
per-request/per-envelope fetch on the droplet, `land.py`'s BLM/USFS/PAD-US pattern (queried
scoped to the coverage envelope, on-demand or on the existing coverage-wide refresh cadence) -
reserved for small, incremental, or interactive sources. Issue #335 PR 3a's first draft picked
the live-fetch pattern for USFS Trail_NFS (a national bulk source) by cloning `land.py` without
checking the issue's own text first - see AGENTS.md's Conventions section.

- **Staging (GitHub Actions, `.github/workflows/bulk-load.yml`, weekly + manual dispatch):**
  `foray stage-snapshot <source>` fetches/transforms a source and uploads it to the DO Space
  under a fresh, run-unique key space (`foray.spaces.snapshot_run_prefix` -
  `bulk/{source}/{date}/runs/{run_id}/...`) - re-staging a date, or two overlapping runs, can
  never collide, since every run gets its own prefix. Only once every object has landed does it
  publish a small manifest naming the current `run_id` (`foray.spaces.publish_snapshot`,
  `bulk/{source}/{date}/_manifest.json`) - the one atomic pointer flip a reader ever observes.
  Runs off the droplet deliberately - a source snapshot can be tens of GB, more disk/bandwidth
  than the 1-vCPU droplet should spend on a job that never touches Postgres. GDAL/`ogr2ogr` in
  the app image (for #335's shapefile/GeoPackage sources) and DuckDB (for querying staged
  Parquet/GPKG directly) are there for the sources that need them; `inat`/`ridb` (below) need
  neither - both stream-filter their source over plain HTTP.
- **Storage format (issue #359):** Parquet is the standard bulk-snapshot format - columnar +
  dictionary/RLE encoding beats the earlier ad hoc gzipped-JSON-Lines pattern for repeated values
  (source names, enums, coordinates), and it's directly queryable with DuckDB. `foray.spaces`
  owns the shared codec (`write_snapshot_parquet`/`read_snapshot_parquet` - a stager hands over
  rows + a `pyarrow.Schema`, a loader gets back the same rows in batches), so every registered
  source uses the same read/write path instead of each hand-rolling its own tempfile/gzip
  dance. A geometry column is WKB bytes, not GeoJSON text (compact, and PostGIS reads it
  natively) - a loader converts it back to the GeoJSON text a table's `ST_GeomFromGeoJSON`
  insert trigger expects (`usfs_trails._wkb_to_geojson`) rather than that format change reaching
  the trigger itself. Retention is one snapshot per source, not history -
  `foray.spaces.prune_other_snapshots` deletes every other object under `bulk/{source}/` right
  after a new run publishes, to keep the monthly Spaces bill from growing unbounded.
- **Loading (droplet, `foray ingest-bulk <source>`):** finds the newest *published* snapshot
  date (`foray.spaces.list_snapshot_dates`, which only counts a date once its manifest exists)
  and skips it if it's not newer than what's already loaded (`meta` key `bulk_snapshot:{source}`).
  A full-table source loads via `foray.ingest_bulk.copy_and_swap` (staging table populated first,
  swapped in on success, never visible half-loaded); `inat`/`ridb` instead upsert into their
  existing table in place (they only ever add/refresh rows for their own `source`/kingdom, never
  own the whole table) - see each loader's docstring for why.
- **Config:** `FORAY_SPACES__ACCESS_KEY_ID`/`SECRET_ACCESS_KEY`/`BUCKET`/`REGION` (`Settings.spaces`,
  see the satellite-overlay section above for the other consumer of this same Space).
- **Staleness visibility (`/healthz/data`, issue #357):** one `bulk-stage:{source}` layer per
  registered source, flagging any whose newest published snapshot is missing or more than 14
  days old (2x `bulk-load.yml`'s weekly cadence) - watches the Space's own published-snapshot
  dates directly, so it would have caught `inat`/`ridb`'s weeks-long unstaged gap the whole time
  it existed. Skipped when Spaces isn't configured (local dev).
- **Registered sources (issue #334 PR 2):**
  - **`inat`** (`foray.sources.inat_bulk`) - iNaturalist's own complete GBIF Darwin Core Archive
    export (`static.inaturalist.org/observations/gbif-observations-dwca.zip`, ~29 GB, regenerated
    weekly - iNaturalist's own developer docs and GBIF's dataset registration both confirm this,
    not daily as earlier docs here assumed), streamed as a single continuous GET and parsed
    forward-only via `stream_unzip` - never downloaded whole. Range reads
    (`foray.sources.http.HttpRangeReader`) were the original approach but got 403'd by
    `static.inaturalist.org`'s CDN partway through a scan (issue found 2026-09-14); iNaturalist's
    own docs say large downloads should go through GBIF instead of this file, and a single GET is
    the same access pattern the retired manual `curl -L` workflow used, which never tripped it -
    see `inat_bulk`'s module docstring for the full writeup. Filtered to `kingdom == "Fungi"` +
    `countryCode == "US"` rows at stage time (no DB needed there); the loader resolves each row's
    genus name to our catalog's genus-level `taxon_id` (`fungi_genera`) and upserts into
    `observations`, exactly like the live `ingest`/`ingest_region` path. Replaces the old manual
    `just bulk-download`/`bulk-filter`/`bulk-load` + `scripts/inat_dwca_filter.py` /
    `load_inat_bulk.py` pair and the one-off `infra/ansible/tasks/deploy/bulk_load_once.yml`
    task - use `just bulk-stage inat` / `just bulk-load inat` (or the scheduled
    `ingest-bulk-inat` job) instead. Chosen over the AWS Open Data dump
    (`inaturalist-open-data`) because that dump only carries `observation_uuid`, never the
    numeric `id` this project's schema keys `observations` on (re-verified live 2026-09-14).
  - **`ridb`** (`foray.sources.camps`) - RIDB's full CSV export
    (`ridb.recreation.gov/downloads/RIDBFullExport_V1_CSV.zip`, public, no API key, refreshed
    at least daily), filtered to camping facilities and upserted into `campsites`, then pruned
    to exactly what the export lists. Fixes the live per-state search's documented ~2/3
    under-return by listing every facility nationally in one pass. Once a `ridb` snapshot has
    ever loaded, `ingest_campgrounds`/`ingest_campgrounds_coverage` (the live per-state/
    coverage-wide RIDB crawl) skip themselves automatically - no separate flag, just a check
    against `meta`.
  - **`usfs_trails`** (`foray.sources.usfs_trails`, issue #335 PR 3a) - the USFS EDW
    `Trail_NFS` foot/stock trail layer (`EDW_TrailNFSPublish_01/MapServer/0`, public, no key),
    queried without a geometry filter (the whole `TRAIL_TYPE='TERRA'` table, ArcGIS
    `resultOffset` paging - 78,149 features as of 2026-09-14) and upserted into `trails`
    (`source='usfs'`, `kind='path'`), pruned to exactly what the export lists. Unlike
    `land.py`'s BLM/USFS/PAD-US ownership layers (queried live, coverage-envelope-scoped, on
    the droplet), issue #335 specifies this source goes through the bulk pipeline rather than
    a live crawl - the stager runs the same ArcGIS paging, just from GitHub Actions instead of
    the droplet. `length_km` is recomputed from geometry, never trusted from the source;
    `attrs` carries `trail_class`/`trail_surface`/`managing_org`/`national_trail_designation`
    plus a `tracktype` grade derived by inverting USFS's 1-5 trail-class scale onto OSM's
    `gradeN` convention.
  - **`usfs_mvum`** (`foray.sources.usfs_mvum`, issue #335 PR 3b) - the USFS EDW Motor Vehicle
    Use Map roads layer (`EDW_MVUM_01/MapServer/1`, public, no key), filtered to `SYMBOL` values
    1/2/3/4/11/12 (the only values the service documents as Forest Service System roads carrying
    OHV-legality data) and upserted into `trails` (`source='usfs_mvum'`, `kind='road'`) - a
    distinct `source` from `usfs_trails`'s `'usfs'` precisely so each loader's
    prune-to-exactly-what's-listed step only ever touches its own rows. `attrs.motor_vehicle` is
    synthesized to `"no"` when none of the standard vehicle classes (`PassengerVehicle`/
    `HighClearanceVehicle`/`Truck`) are open but at least one vehicle-class field carries data -
    reusing the OSM-derived `motor_vehicle`/`access` vocab `scoring.queries._walk_in` already
    reads, so that function needs no source-specific branch. `attrs.tracktype` inverts
    `OperationalMaintLevel` (1 primitive -> 5 paved) the same way `usfs_trails` inverts
    `TRAIL_CLASS`. OSM/USFS dedup happens at read time, not ingest: `trails_near`/`nearest_trail`
    drop an OSM `path`/`road` row whenever a `source LIKE 'usfs%'` row of the same `kind` sits
    within 15 m (`scoring.queries._USFS_DEDUP_FILTER`) - the USFS layer is authoritative
    (access matrix, official class/name) where OSM is a crowdsourced guess.
