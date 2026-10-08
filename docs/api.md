# HTTP API reference

The web app is a thin client over a JSON API served by the same FastAPI process that serves the
built frontend. Everything the UI does goes through the routes below, so they are also the way to
script Foray Planner or build another client.

- **Base URL:** `https://forayplanner.com` in production, `http://localhost:8000` from
  `just start` / `uv run foray serve`.
- **Interactive docs:** FastAPI serves Swagger UI at `/docs` and the raw schema at
  `/openapi.json`. `uv run foray openapi` prints the same schema without starting a server (it is
  what generates the frontend's `schema.ts`, see [frontend.md](frontend.md#typed-api-client)).
- **Source of truth:** the route modules in [`src/foray/api/routes/`](../src/foray/api/routes) and
  the response models in [`src/foray/api_models.py`](../src/foray/api_models.py). If this page and
  the code disagree, the code wins; please fix this page.

> The API makes no identification, edibility or safety claims, and no endpoint asserts that
> camping is legal anywhere. Land ownership and camp data are informational; each row links its
> official source.

---

## Conventions

### Anonymous device identity

There are no accounts. The first request from a browser that carries no `device_id` cookie gets
one back (`Set-Cookie: device_id=...; HttpOnly; SameSite=Lax; Secure` over HTTPS, one year). That
opaque id is the key for the visitor's two pieces of saved state:

| State | Table | Routes |
|---|---|---|
| Saved home + search radius | `app_location` | `POST` / `DELETE /api/location` |
| Selected target genera | `app_genera` | `/api/genera/selected`, `POST` / `DELETE /api/genera/{taxon_id}` |

A client that does not keep cookies (a script, `curl`) starts as a new visitor on every request:
it sees the server's default home and an empty genus selection (which means "everything nearby").
Pass `species` and explicit `lat` / `lng` parameters instead of relying on saved state.

### Shared query parameters

| Parameter | Meaning |
|---|---|
| `months` | Comma-separated month numbers `1`-`12` (for example `9,10,11`). Omitted: the current calendar month. An empty string means all twelve. Out of range: `400`. |
| `species` | Comma-separated iNaturalist **taxon ids** of genera, or `all` (the default). `all` expands to the device's saved selection; an empty selection means no filter, so every fungus counts. A non-integer token: `400`. |
| `radius_km` | Search radius around the centre point. Defaults to the visitor's saved radius (or the server default, 150 km) on the ranking routes, and to a layer-specific default on the point routes. |
| `region_id` | An **H3 cell id** (a 15-character hex string such as `8428d55ffffffff`) taken from a `/api/destinations` row. A malformed id: `400`. |
| `weeks` | Replaces the month filter with a trailing window of that many weeks (the "Active now" signal). |

Regions are [H3](https://h3geo.org) hexagons at the server's configured resolution (4 by default,
about 26 km edge length, roughly 1,770 km² a cell). See [architecture.md](architecture.md#regions-and-the-h3-grid).

### Status codes

| Code | When |
|---|---|
| `200` | Success. |
| `400` | A malformed parameter (`months`, `species`, `region_id`), or a point route called without `region_id` or both `lat` and `lng`. |
| `404` | An unknown place (geocoding), trail id, or non-public observation id; or a vector tile route when tiles are not configured. |
| `409` | Reads are paused (`"refreshing data for this area - try again shortly"`) while a refresh rebuilds the phenology tables, or the database has no data yet (`"no data for this area yet"`). Retry shortly. |
| `413` | A `POST` body over 32 KiB. |
| `422` | A parameter that fails validation (range, length, pin or waypoint shape). |
| `429` | `POST /api/refresh` rate limit (one per client IP per 300 s); carries `Retry-After`. |
| `502` / `503` | An upstream (Nominatim, Esri, iNaturalist, the tile server) is unavailable, or `/healthz/data` reports a stale layer. |

### Caching

| Responses | `Cache-Control` |
|---|---|
| Ranking and observation reads (`/api/destinations`, `/api/calendar`, `/api/alerts`, `/api/observations/precise`, `/api/observations/photos`) | `private, no-cache` (per device; always revalidate) |
| Per-observation thumbnail | `public, max-age=86400` |
| Destination satellite image and labels | `public, max-age=86400` |
| Vector tiles (`/api/tiles/{trails,land,fire}`) | `public, max-age=3600` |
| Satellite basemap tiles | `public, max-age=604800` |

Ranked results are also held in an in-process cache for `FORAY_OBSERVABILITY__RANKING_CACHE_TTL_SECONDS`
(default 600 s) and dropped whenever phenology is rebuilt.

### Limits and hardening

- Request statement timeout: each API database connection runs with `statement_timeout = 5s`; a
  query that exceeds it fails rather than holding a pooled connection.
- Request bodies are capped at **32 KiB**; only `POST /api/location` and
  `POST /api/observations/thumbnails` accept a body.
- No CORS headers are sent, on purpose: the API is same-origin only.
- Every response carries a restrictive Content-Security-Policy, `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy` and `Permissions-Policy` (see
  [`api/security.py`](../src/foray/api/security.py)).

---

## Configuration and identity

### `GET /api/config`

Per-visitor home plus the server's map and tile settings. The frontend calls this first.

```json
{
  "home": {"name": "Bend, Oregon", "lat": 44.0582, "lng": -121.3153, "radius_km": 150.0},
  "region_radius_km": 26.07,
  "recent_weeks": 4,
  "refreshing": false,
  "rebuilding_phenology": false,
  "last_error": null,
  "basemap_url": "https://.../basemap.pmtiles",
  "terrain_url": "https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png",
  "satellite_tiles_url": "/api/tiles/satellite/{z}/{x}/{y}.jpg",
  "trails_tiles_url": "/api/tiles/trails/{z}/{x}/{y}.pbf",
  "land_tiles_url": "/api/tiles/land/{z}/{x}/{y}.pbf",
  "fire_tiles_url": "/api/tiles/fire/{z}/{x}/{y}.pbf"
}
```

`region_radius_km` is the real-world size of one region cell, so the client can draw a selected
destination's circle without an H3 library. The three vector-tile URLs are blank unless a martin
tile server is configured (`FORAY_MARTIN_URL`); `basemap_url` blank means the map has overlays but
no base layer.

### `POST /api/location`

Save this device's home and search radius. Send **one** of:

```json
{"query": "Coos Bay, OR"}
{"lat": 43.37, "lng": -124.21}
```

Optional on both: `name` (at most 200 characters) and `radius_km` (greater than 0, at most 500;
defaults to the current radius). With bare coordinates the server reverse-geocodes a display name
(falling back to `"43.3700, -124.2100"` if that lookup fails). Returns `{"home": {...}}`. Saving a
location never triggers an ingest; it only changes what the next read scores against.

Errors: `400` with neither `query` nor both `lat`/`lng`; `404` if `query` matches nothing; `502`
if the geocoder is down.

### `DELETE /api/location`

Forget this device's saved home and radius (back to the server default). Returns
`{"status": "deleted"}`. Only ever touches the caller's own row.

### `GET /api/location/search?q=...`

Typeahead place search, proxied to OpenStreetMap Nominatim so the browser never calls it directly.
Returns `[{"name", "lat", "lng"}]`; an empty array for a blank query or any geocoder failure, so
autocomplete degrades quietly.

### `GET /api/genera?q=...`

Search the Fungi genus catalog (about 6,000 genera). An empty `q` returns the most-observed
genera. Each hit: `{"taxon_id", "name", "common_name", "icon"}`. `common_name` is usually `null`
(most genera have no English name on iNaturalist); `icon` is the key of the icon drawn on the map
and cards (see [frontend.md](frontend.md#genus-icons)).

### `GET /api/genera/selected`

This device's selected target genera, same shape as above. Empty means "everything nearby".

### `POST /api/genera/{taxon_id}` and `DELETE /api/genera/{taxon_id}`

Add or remove one genus from the device's selection. Return `{"status": "added"}` /
`{"status": "removed"}`. Both are idempotent.

### `GET /api/coverage`

Each configured coverage region (all 50 US states by default) with its latest observation ingest:
`[{"name", "place_id", "last_ingest", "observations_ingested"}]`. `last_ingest` is an ISO timestamp
or `null`.

---

## Ranked destinations

### `GET /api/destinations`

The ranked list behind the main panel. Parameters: `months`, `species`, `radius_km` (all
optional, see above). Returns an array of regions, best first:

| Field | Meaning |
|---|---|
| `region_id`, `center_lat`, `center_lng` | The H3 cell and its centre. |
| `distance_km` | Great-circle distance from the visitor's home. |
| `score`, `score_norm` | Raw score and the score normalised 0..1 against the top region. |
| `n_species`, `species[]` | Number of genera, and each genus's `taxon_id`, `name`, `common_name`, `icon`, `month_count` (records in the chosen months), `total_count` and `w_pheno` (the share of its local season that falls in those months). |
| `recent_count` | Records inside the trailing `recent_weeks` window. |
| `pheno_trend` | `peak`, `building`, `past-peak`, `off` or `null`: where the chosen months sit in the top genus's local season. Informational; not part of the score. |
| `elevation_m` | Mean ground elevation of the region's observations, metres (`null` until enriched). |
| `precip_obs_7d_mm`, `precip_obs_30d_mm` | Mean rainfall in the 7 / 30 days before the region's observations were made. |
| `precip_recent_7d_mm`, `precip_recent_14d_mm`, `precip_recent_30d_mm` | Rain that fell at the cell recently. |
| `fire_nearby[]` | Active fires and recent burn scars near the cell (`name`, `status`, `distance_km`, `incident_url`, `dominant_severity`, ...). |
| `trailhead_km`, `camp_km`, `camp_is_free` | Distance to the nearest cached trailhead and campground; `null` means none cached within 45 km (unknown, not "none exists"). |

How the score is built, and what the adjustments do, is in [scoring.md](scoring.md).

### `GET /api/alerts`

The "Active now" list: regions where a selected genus was observed in the trailing window.
Parameters: `species`, `weeks` (default: the server's `recent_weeks`, 4), `radius_km`. Returns
`[{region_id, center_lat, center_lng, distance_km, total, species[], precip_recent_*, fire_nearby[]}]`
where each `species` entry carries `count`, `last_seen`, `place_guess`, `uri` (the iNaturalist
observation) and `obscured` (the observer fuzzed the location). Returns `[]` if the database has
no data yet instead of `409`.

### `GET /api/calendar?region_id=...`

The 12-month heatmap behind **Details > Calendar**: an object keyed `"1"` to `"12"`, each
`{"total": n, "species": {"Cantharellus": n, ...}, "icons": {...}}`. Takes `species`.

### `GET /api/observations/photos?region_id=...`

Recent research-grade observations for a region with their Creative Commons photos, newest first.
Parameters: `region_id` (required), `species`, `months`, `weeks`, `offset` (default 0). Returns
`{"observations": [...], "has_more": bool}`, twelve observations per page. Only photos with a
re-displayable licence are included, each with `url`, `license_code` and `attribution`.

### `GET /api/observations/precise`

Verified-location observation pins (not obscured) for the map. Parameters: `species`, `months`,
`weeks`, and either both `lat` and `lng` (a focused destination) or neither (the visitor's home);
`radius_km`. Supplying only one of `lat`/`lng` is a `400`. Each pin carries `id`, `lat`, `lng`,
`observed_on`, the genus `name` / `icon`, the finer `taxon_name` / `taxon_common_name` when known,
and the iNaturalist `uri`.

### `GET /api/observations/{obs_id}/thumbnail`

One Creative Commons photo for a pin's popup, or `null` when iNaturalist has none we may show. The
first request fetches it from iNaturalist and caches the answer (including "none") for 30 days.
Only observations already cached as precise are served (`404` otherwise), so this cannot be used
to proxy arbitrary iNaturalist lookups.

### `POST /api/observations/thumbnails`

Warm and return photos for many pins in one call: body `{"ids": [123, 456, ...]}` (at most 300).
Returns `{"thumbnails": {"123": {...} | null, ...}}`; an absent id was not resolved and the client
falls back to the per-pin route. The batch never queues behind another prefetch.

### `GET /api/destinations/places?region_ids=a,b,c` and `GET /api/destinations/{region_id}/place`

A card's title is the most notable named place near the cell centre. The batch route returns only
the names already cached (`{"<region_id>": {"place_name": "..."}}`); the single route does the
throttled Nominatim lookup for one region if needed and caches the result, including "nothing
notable" (`{"place_name": null}`). A transient geocoder failure is not cached.

---

## Camps, trails, public land

All three take either `region_id`, or both `lat` and `lng` (otherwise `400`).

### `GET /api/camps`

Campsites near a point, free first and then nearest. Parameters: `radius_km` (default 40),
`free_only` (default false), `limit`. Each row: `id`, `name`, `kind`, `source`
(Recreation.gov or OpenStreetMap), `free` (`true` only on an explicit no-fee signal, otherwise
`null`; never guessed), `fee`, `fee_low` / `fee_high` (nightly range parsed from the fee text),
`reservable`, `camp_type` (`tent`, `rv`, `mixed`, `backcountry`, `group`, `equestrian`, `cabin`,
`pitch`), `pitch_count`, `distance_km` and `url` (the official page).

### `GET /api/trails`

Trails near a point. Parameters:

| Parameter | Default | Meaning |
|---|---|---|
| `radius_km` | 40 | Search radius. |
| `kind` | all | `trailhead`, `path`, `road` (forest road) or `route` (named hiking route). |
| `limit` | none | Cap after sorting. |
| `sort` | `nearest` | `nearest`, `relevance` (named routes, length and nearby target-genus finds first) or `longest`. |
| `significant_only` | false | Drop unnamed OSM connector stubs. |
| `distinct_names` | true | Collapse to one row per trail name. |
| `species` | `all` | Which genera the relevance term counts. |

Geometry is omitted from this list on purpose. Selecting a row fetches
`GET /api/trails/network?trail_id=...` for that one trail, which returns
`{"trail": {..., "geometry": GeoJSON}, "authoritative": bool}` (`404` if nothing is found).
`trail_id` is a query parameter because ids contain a slash (`osm:node/123`).

Row fields include `length_km`, `attrs` (surface, access and other kept OSM tags), `walk_in`
(a road gated to vehicles but walkable), `land_agency` / `land_unit` (who manages the ground it
crosses), `forage_obs` (research-grade fungi records within about 500 m of the line, a hint rather
than a score) and `camp_distance_km`.

### `GET /api/land`

Pinnable public-land parcels near a point, nearest first, ownership only (the map draws parcels
from vector tiles). Required: `lat`, `lng`. Optional: `radius_km` (default 30, at most 200),
`limit` (default 20, at most 50). Each parcel: `id`, `agency`, `unit`, `url`, `distance_km`.
Only federal and state land managers are offered; tribal, military and city land is map-only.

---

## Trip planning

### `GET /api/plan`

A start-to-destination trip with stops along a straight-line corridor. This is **not** road
routing; legs are great-circle distances. See [scoring.md](scoring.md#trip-planner).

| Parameter | Default | Meaning |
|---|---|---|
| `months`, `species` | current month, `all` | As above. |
| `start` | the visitor's home | A place name or `lat,lng`. |
| `destination` | auto | A place name or `lat,lng`. Omitted: the best-scoring reachable region. |
| `corridor_km` | 60 | How far off the straight line a stop may be. |
| `max_stops` | 5 | 1 to 20. Shapes an auto-picked trip only. |
| `max_drive_km` | 400 | Longest single leg. |
| `camp_radius_km` | 40 | How far to look for a camp near each stop. |
| `require_free_camp` | false | Keep only stops with a free camp nearby. |
| `waypoints` | none | Ordered, comma-separated region ids (at most 20). When present these are the **whole** itinerary and nothing is auto-filled around them. |
| `pin` | none | Repeatable. `<region_id>:<camp\|trail\|land>:<feature id>` pins a waypoint to a specific cached campground, trail or parcel. At most one per waypoint; coordinates are looked up server-side, never taken from the client. |

Returns `{start_lat, start_lng, destination_lat, destination_lng, destination_name,
auto_destination, corridor_km, months, n_stops, total_drive_km, stops[], skipped_unreachable}`.
Each stop has `order`, the region summary (`score_norm`, `species[]`, `recent_count`),
`progress_km`, `drive_km_from_prev`, `cumulative_drive_km`, the nearby `camp` and `trail`
(with their distances), `fire_nearby[]` and, when pinned, `pin` (`kind`, `id`, `name`,
`feature_kind`, `lat`, `lng`).

Errors: `404` if `start` or `destination` cannot be resolved, `422` for malformed `waypoints` or
`pin` values.

`foray plan` runs the same planner from the command line.

---

## Refreshing data

The **Refresh** button re-pulls iNaturalist and the area layers around the visitor's home in a
background thread. Only one refresh runs at a time, process-wide.

| Route | Behaviour |
|---|---|
| `POST /api/refresh?target=...` | `target` is `mushrooms` (default), `camps`, `land`, `dispersed`, `trails` or `all`. Returns `{"status": "started"}` or `{"status": "already running"}`. `400` for an unknown target; `429` if the same IP refreshed within 300 s. A `mushrooms` or `all` refresh rebuilds phenology, during which the read routes answer `409`; a layer-only refresh leaves reads open. |
| `DELETE /api/refresh` | Cancel. Returns `{"status": "cancelling"}` or `{"status": "idle"}`. |
| `GET /api/refresh/stream` | Server-Sent Events. Each `data:` line is JSON: `{"step": "...", "progress": 0-100}` while running, then a final `{"step": "Done", "progress": 100, "done": true}` or `{"error": "...", "done": true}`. A late subscriber first receives the most recent message. |

Scheduled ingestion does not use these routes; see [jobs.md](jobs.md).

---

## Map tiles

| Route | Returns |
|---|---|
| `GET /api/destinations/{region_id}/satellite/image` | The Esri imagery stitched for one destination's footprint (`image/jpeg`). |
| `GET /api/destinations/{region_id}/satellite/labels` | Its transparent roads-and-labels overlay (`image/png`). |
| `GET /api/tiles/satellite/{z}/{x}/{y}.jpg` | One Esri World Imagery tile for the full-map satellite basemap, proxied so the page's CSP can stay `'self'`. Zoom 0 to 19; otherwise `400`. |
| `GET /api/tiles/trails/{z}/{x}/{y}.pbf` | Trails and forest roads as Mapbox vector tiles. |
| `GET /api/tiles/land/{z}/{x}/{y}.pbf` | Public-land ownership polygons. |
| `GET /api/tiles/fire/{z}/{x}/{y}.pbf` | Wildfire perimeters and burn scars. |

The three `.pbf` routes proxy a [martin](https://martin.maplibre.org) tile server and answer `404`
when `FORAY_MARTIN_URL` is unset. The base map itself is a Protomaps PMTiles archive the browser
reads directly from `basemap_url`; it is not served by this API. See
[data-sources.md](data-sources.md).

---

## Health and metrics

None of these need a cookie, and none are rate limited. In production the proxy answers `404` to a
public request for `/metrics` (only the on-host metrics collector may scrape it); the three
`/healthz*` routes are reachable publicly, so an external uptime monitor can poll them.

| Route | Purpose |
|---|---|
| `GET /healthz` | Liveness only. No database round trip, so a Postgres blip does not get the container killed. Returns `{"status": "ok"}`. |
| `GET /healthz/data` | Freshness. One entry per data layer (`observations`, `land`, `trails`, `dispersed`, `camps` when `RIDB_API_KEY` is set, `fire`, `precip`, and a `bulk-stage:<source>` entry per staged bulk source when object storage is configured) with `last_success`, `interval_hours`, `stale` and `blocking`. A layer's `last_success` is the newer of its newest ingest-log entry and its job's newest successful run. `503` when any **blocking** layer is older than its interval times `FORAY_OBSERVABILITY__DATA_FRESHNESS_MULTIPLIER` (default 2). |
| `GET /healthz/backlog` | Queue depth and drain rate for the elevation and precipitation backfills. Informational; never `503`. |
| `GET /metrics` | Prometheus text exposition: job run counts, durations, rows and rate-limit hits by job; backfill depth and drain rate; layer age and staleness. Queried from Postgres on every scrape. |

What they are for, and how production wires them to alerts, is in [jobs.md](jobs.md#observability).

---

## Static client

`GET /` serves the built single-page app (`src/foray/web/dist/`, produced by `just frontend`), and
`/assets/*` plus the PWA manifest, service worker and icons are served from the same directory.
