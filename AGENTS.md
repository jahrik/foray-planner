# AGENTS.md - Foray Planner

Python web app that ranks mushroom-hunting destinations from iNaturalist observation
phenology. Public repo: [jahrik/foray-planner](https://github.com/jahrik/foray-planner).

## Why this app exists

Built for a mycology hobbyist who travels constantly and tracks finds on iNaturalist - turns
iNat phenology into road-trip planning: where to go next to be standing on top of the mushrooms,
and where to sleep for free on BLM/Forest Service land while there. Jobs to be done, in
priority order: (1) when/where are target fungi active now/soon, (2) where to camp for free,
closest to that activity, (3) which trails put you closest to the mushrooms, (4) string several
week-long stays into a sane driving route.

Guiding principles - keep these in mind for any feature work:
- **Free-first.** Rank dispersed camping on public land above paid sites; surface cost when known.
- **No claims.** No edibility/ID/safety claims - and don't *assert* camping legality either. Show
  land ownership + link the official source; informational, not authoritative.
- **Reuse the grid.** Camping and trails hang off the same lat/lng grid + `haversine_km` scoring
  already uses - don't invent a second geography.
- **No real road routing yet.** `plan_route`'s corridor is a straight-line (great-circle chord)
  buffer, not an actual road route - there's no routing engine (OSRM/Valhalla/etc.) wired up.
  Self-hosting one is a deliberate future follow-up once a region scope is picked, not something
  to bolt on ad hoc.

## Layout

`src/foray/` is grouped into subpackages (issue #242): `sources/` (every external-data client
plus the ingest layer built on them), `scoring/` (region ranking, the read repository, the trip
planner), `api/` (FastAPI). Root-level modules are the shared leaves: `config`, `defaults`,
`cache`, `geo`, `api_models`, `logging_config`, `refresh`, `cli`.

- `src/foray/config.py` - pydantic-settings (`Settings(BaseSettings)`) with `Home`, `Ingest`,
  `CoverageRegion` models. All config comes from env vars (prefix `FORAY_`, nested
  delimiter `__`) or `.env` file. The runtime location override lives in Postgres
  (`app_location` table, `foray.cache.load_location`/`save_location`), not a file. There's no
  fixed target-genus list (issue #79) - the full Fungi catalog lives in `fungi_genera`
  (refreshed via `foray genera-refresh`), and each device picks its own targets in
  `app_genera`.
- `src/foray/defaults.py` - built-in home location and coverage regions (WA/OR/ID).
  Overridden via `FORAY_COVERAGE` env var.
- `src/foray/sources/inat.py` - throttled pyinaturalist wrapper (observations, fungi-genera
  catalog, photos). Descriptive User-Agent; deep-paginates via `id_above`; `_with_retries`
  backs off on transient network errors so one blip doesn't abort a long ingest.
- `src/foray/sources/geocode.py` - resolve a place name (OpenStreetMap Nominatim) or raw `lat,lng`
  to coordinates: `resolve` (one hit), `suggest` (typeahead list, backs
  `GET /api/location/search`), `reverse` / `notable_place_name`. Network-mocked in tests.
- `src/foray/geo.py` - pure lat/lng math shared by scoring + the ingest layer: `haversine_km`
  (canonical distance), `bbox_around` (the flat-degree disk bbox every `*_near` query
  prefilters with), `KM_PER_DEG_LAT`, and the corridor tangent-plane projection helpers.
- `src/foray/sources/http.py` - shared HTTP plumbing for the external-data modules: `USER_AGENT`,
  `Throttle` (process-wide request pacer), `retry_after_seconds` (`Retry-After` parsing +
  capped backoff), and `SOURCE_ERRORS` (the "log + degrade to empty" exception tuple).
- `src/foray/sources/overpass.py` - shared OSM Overpass client (`ENDPOINTS` list with failover,
  `around()`/`bbox()` filter fragments, `post()` with 429/504 backoff then next-mirror) used by
  `sources/dispersed.py` and `sources/trails.py`. `FORAY_OVERPASS_URLS` overrides the list.
- `src/foray/cache.py` - Postgres schema (tables created eagerly on every `connect()`) +
  idempotent upserts (`ON CONFLICT`), ingest log. `connect()` takes no DSN by default - reads
  the standard `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` env vars.
- `src/foray/sources/ingest_base.py` - `run_area_ingest(...)`: the shared skeleton (skip-if-covered ->
  fetch -> upsert -> record) behind the four home-radius area ingests (campgrounds, dispersed,
  land, trails). A new area source is a fetch function + an upsert function.
- `src/foray/sources/ingest.py` - pulls per seed taxon within the home radius or by coverage region
  (`place_id`). Tags each obs with the **seed** taxon_id (not leaf species) so phenology is
  per foraging target. `ingest` / `ingest_region` share `_consume_observations` (scan ->
  resolve genus -> chunked upsert, 5000 rows, with progress + abort) for bounded memory.
  `revalidate()` is a separate, recurring re-check pass: a handful of fungal genus names are
  homonyms of common animal genera (fungal *Olla* vs. the ladybug genus, etc), so observations
  occasionally get cached under the wrong (non-fungal) taxon_id and never self-correct since
  `ingest`/`ingest_region` only ever revisit a narrow incremental overlap window. It targets
  only genus taxon_ids flagged by `cache.suspect_genus_taxon_ids` (cached-count vs.
  `fungi_genera.observations_count`, DB-only, no iNat call) and re-fetches just those cached
  observations to purge/reassign anything no longer Fungi. `resync()` is the slower complement:
  a whole-table grind, one small batch per call, oldest/never-live-checked first
  (`cache.stale_observation_ids`, driven by the `revalidated_at` column both functions stamp via
  `cache.mark_revalidated`) - it's the only path that eventually re-verifies every column of
  every row, including `obscured` (never set by the bulk historical import) and
  misidentifications too rare within their genus for `revalidate`'s ratio to flag. Both share
  the actual re-check/purge/reassign logic (`_recheck_ids`).
- `src/foray/sources/camps.py` - developed-campground ingest from the Recreation.gov **RIDB API**
  (httpx, key from env `RIDB_API_KEY`). Pages `facilities?state=XX&full=true` for every state the
  home disk reaches, clips to the true radius with `haversine_km` (RIDB's point+radius search
  under-returns ~2/3; radius tiling is a non-US fallback). `Reservable` + a best-effort `fee_low`
  /`fee_high` parsed from the fee prose (add-ons/discounts dropped). Skipped (no-op) when the key
  is unset. `free` only on an explicit no-fee signal *and* no positive fee amount - never guessed.
  `prune_campsites_outside_radius` clears stale out-of-radius rows after each ingest.
- `src/foray/sources/dispersed.py` - dispersed-camping layer from OSM **Overpass** (httpx, no key).
  One ODbL signal, cached as `campsites` (`kind='reported'` - `tourism=camp_site`/`camp_pitch`,
  `backcountry=yes`). `free=TRUE` only on an explicit no-fee tag, never guessed; the *legality*
  caveat rides on `kind`+UI label, never asserted. (A `public_land`-proxy signal - unmapped roads
  within public land, inferred as likely dispersed sites - was scoped but never implemented; see
  issue #110.)
- `src/foray/sources/trails.py` - trail layer from OSM **Overpass** (httpx, no key). One ODbL request
  pulls backcountry paths (`highway=path`/`bridleway` -> `kind='path'`; `footway` **excluded** -
  mostly urban sidewalks), forest / logging roads (`highway=track`, and `highway=service` +
  `service=forestry` -> `kind='road'` - a primary foraging surface, kept separately queryable),
  named hiking routes (`route=hiking` relations -> `kind='route'`, member ways stitched), and
  trailheads (`highway=trailhead` nodes -> `kind='trailhead'`, Point). The route clause needs
  its **own** `out geom;` - inside a union `out geom` drops relation members. At ingest each
  trailhead is snapped (<=35 m) onto the payload's trail polylines and the matched ids (expanded to
  every segment sharing a `_group_key` - `name`, or `ref`+`operator` for an unnamed forest road)
  stored in `trails.connects`, so `resolve_trail_network` draws the whole named trail / whole
  numbered road from cache; a live per-selection query is the fallback and its result is written
  back. Also derived at parse: `length_km` and `attrs` (highway/surface/tracktype/access/
  motor_vehicle/foot/ref/...). Geometry cached as GeoJSON *text* + `geom` GIST + a representative
  center. The per-region one-shot marker is keyed `trails:place:{id}:q{_TRAILS_QUERY_VERSION}` -
  bump that constant when the Overpass query changes and the weekly `refresh --with trails --all`
  cron re-pulls every region on its own (superseded markers pruned on success). `--force` is a
  manual re-pull without a version bump (OSM drift / debugging one region).
- `src/foray/sources/fire.py` - wildfire perimeters + burn scars from NIFC / MTBS ArcGIS
  (httpx, no key, issue #227), cloned from `land.py`. One table `fire_perimeters`, split by
  `source_key` into two refresh lanes: `wfigs_active` (fast, **replace semantics** -
  `cache.replace_fire_lane` deletes rows the source dropped, so a contained fire falls off)
  and `perimeter_history` (slow upsert, last 3 completed fire years + current). MTBS burn
  severity (`dominant_severity`) is a backfill-style join by IRWIN/MTBS id, NULL until MTBS
  publishes (~1.5-2 yr out). Informational only - links the incident page, asserts no closure.
  `scoring.fire_near` reads it; `rank_destinations` applies an active-fire proximity penalty
  and a *Morchella*-scoped burn-scar boost (severity-aware) - weights in `ranking.py`.
- `src/foray/scoring/` package (was one `scoring.py`; its `__init__.py` re-exports the public
  surface so `from foray.scoring import ...` and `foray.scoring.<name>` keep working):
  - `models.py` - the result dataclasses (`SpeciesHit`, `RegionScore`, `CampSite`, `LandUnit`,
    `Trail`, `TrailPath`, `Stop`, `TripPlan`); mirrored by `api_models.py`. `RegionScore` carries
    `pheno_trend` (see `trend.py`).
  - `regions.py` - `build_phenology` (materializes `regions` + `phenology`) plus the
    materialized-table helpers (`region_elevations`, `region_precip_obs`, ...).
  - `ranking.py` - `rank_destinations` / `rank_destinations_corridor` (fix months -> rank
    regions). After the phenology rank, multiplicative adjusters fold in fire (`_apply_fire`)
    and trail/camp **access** (`_apply_access`, issue #306): a trailhead within 3 km boosts,
    no trailhead *and* no camp within 15 km penalises, `None` (nothing cached within 45 km)
    means "unknown" and is left alone. Distances land on `RegionScore.trailhead_km`/`camp_km`.
  - `trend.py` - `phenology_trend`: for each ranked region's **top** genus, classifies where the
    selected months sit in that genus's local season - `peak` / `building` / `past-peak` / `off`,
    from its month-by-month observation histogram for the region. Coarse, informational only - it
    is **not** an input to the score; the frontend's card "why" sentence uses it (issue #301).
  - `queries.py` - the point-and-radius read repository: `camps_near`, `land_near`,
    `trails_near` (`sort=nearest|relevance|longest`; relevance = named-route + connected length +
    log-scaled target-genus obs within 500 m of the line, and for `kind='road'` re-weighted so
    obs-density dominates + a gated-but-walkable road (`_walk_in`, surfaced as `Trail.walk_in`)
    scores a bonus; `significant_only` hides unnamed OSM stubs), `get_trail`, `connected_trails`,
    `nearest_trail`, `region_access`, `place_calendar`,
    `recent_observations`, `alerts` (includes `place_guess` / `uri` / `obscured` per obs),
    `precise_observations`.
  - `planner.py` - `plan_route` (start -> destination corridor trip: stops along the
    straight-line buffer, each annotated with a nearby camp *and* trail, ordered by progress;
    auto-picks a destination when the caller doesn't - see "no real road routing yet" below).
    Optional `waypoints` (ordered region ids, from the frontend's "+ Plan" shortlist, issue #301)
    are threaded in as **required** stops - never dropped for score, a missing free camp, or an
    over-long leg; the corridor widens to include every pick and, with no explicit destination,
    the trip runs to the farthest waypoint. `GET /api/plan?waypoints=` bounds-checks each id's
    implied cell centre before the corridor math.
  - `_sql.py` - the SQL fragments shared by the three query modules (grid binning
    `BINNED`, the decoy-aware center expressions, the `taxon_id` / `IN (...)` helpers,
    `genus_name_map`).
- `src/foray/api/` - FastAPI package (was one `api.py`; issue #242 Part 1e). `/api/{config,
  genera,destinations,calendar,alerts,camps,land,trails,plan,location,refresh,coverage}` + `/`
  (serves the built client). Search is **read-only** against cached data. `set_location` does not
  trigger refresh. A `psycopg_pool.ConnectionPool` opened/closed via FastAPI `lifespan`; `refresh`
  runs in a background thread with SSE progress.
  - `app.py` - `create_app()`: builds the `FastAPI`, opens the pool + `AppState` onto `app.state`,
    registers the routers in OpenAPI-schema order (`foray openapi` output is drift-checked).
  - `routes/*.py` - one `APIRouter` per domain (`config`, `genera`, `coverage`, `destinations`,
    `layers`, `plan`, `location`, `refresh`, `index`).
  - `deps.py` - shared request helpers as module functions: `get_pool` / `get_state` accessors,
    anonymous device-id resolution, `resolve_home` / `resolve_genera`, `parse_months` /
    `parse_species`, `region_center`, rate limiting.
  - `state.py` - the `AppState` dataclass. `security.py` - CSP + security headers + body-size cap.
    `refresh_runner.py` - the API-side wrapper around `run_home_refresh`: background thread,
    shared HTTP client, cancellation, SSE progress broadcast. `paths.py` - `web/dist`.
- `src/foray/refresh.py` - the ingest-refresh sequence shared by the CLI and API paths:
  `run_home_refresh(cfg, conn, layers, *, client, abort_event, progress_cb)` (home-radius
  ingest per layer + phenology rebuild), plus `REFRESH_LAYERS` / `REFRESH_TARGETS` and
  `parse_month_list`. The CLI's coverage-wide `foray refresh --all` is a separate per-region
  sequence and stays in `cli.py`.
- `src/foray/logging_config.py` - `setup_logging(level)` (env `FORAY_LOG_LEVEL`, default INFO),
  called by both the CLI group callback and `create_app`.
- `src/foray/cli.py` - Click CLI: `foray ingest | camps | land | dispersed | trails | refresh |
  revalidate | resync | genera-refresh | backfill-elevation | backfill-precip | refresh-precip |
  backfill-satellite | fire | plan | serve | openapi`. `ingest --all-regions` is what the
  scheduler runs. `backfill-elevation` fills
  `observations.elevation_m` for the backlog (ingest enriches new rows inline via Open-Meteo);
  destination cards show the region's mean. `backfill-precip` does the same for
  `observations.precip_7d_mm` / `precip_30d_mm` (antecedent rainfall, issue #226, ERA5 archive);
  `refresh-precip` rebuilds the recent-rain-per-destination layer from the forecast API.
  `fire` refreshes the wildfire perimeters / burn scars / MTBS severity (issue #227).
  `resync --until-done` loops batch after batch until the whole cache is caught up
  (`just resync "--until-done --batch-size 20000"`) - a deliberate one-off catch-up run,
  not the small-batch/hourly default the scheduler uses.
- `scripts/scheduler.sh` - shell loop running observation ingest (all regions), layer refresh,
  observation revalidation (`foray revalidate`, see `ingest.py`), the whole-table resync
  grind (`foray resync --batch-size N`), and the elevation backfill drain
  (`foray backfill-elevation --limit N`, issue #36 - Open-Meteo rate-limits a burst so each
  pass only does a few hundred rows), and the rainfall pass (`foray backfill-precip` +
  `foray refresh-precip`, issue #226), each on their own N-hour interval. Configurable via
  `FORAY_INGEST_INTERVAL_HOURS` (default 24), `FORAY_LAYERS_INTERVAL_HOURS` (default 168),
  `FORAY_REVALIDATE_INTERVAL_HOURS` (default 168), `FORAY_RESYNC_INTERVAL_HOURS` (default 1),
  `FORAY_RESYNC_BATCH_SIZE` (default 2000), `FORAY_ELEVATION_INTERVAL_HOURS` (default 1),
  `FORAY_ELEVATION_LIMIT` (default 20000 - an upper bound; a run stops earlier when Open-Meteo
  rate-limits it), and `FORAY_PRECIP_INTERVAL_HOURS` (default 24 - rain changes far faster than
  the 168h layers), and `FORAY_FIRE_INTERVAL_HOURS` (default 24 - perimeter data updates ~daily).
- `frontend/` - the web client: **Vite + TypeScript (strict)**, Leaflet map, split by concern
  into `src/{map,ui,views,api}/` subfolders. The #301 redesign reshaped the UI into one
  answer-first flow (no tabs); the pieces:
  - `src/tokens.css` - the visual identity (issue #301): a spore-print colour palette
    (`--rust` / `--purple` / `--moss` / `--flush` reserved for recency / `--spore` for
    verified-location pins) and a ~1.25 modular type scale. Legacy token names
    (`--bg`/`--panel`/`--accent`/...) are remapped onto it so `style.css` reskins without a
    sweep; large-text mode scales the `--text-*` tokens here. Fraunces (display) + IBM Plex Sans
    (body/data) are self-hosted via `@fontsource-variable`, bundled into the build, no CDN.
  - `src/state.ts` - the one flat `state` object; its `State` type is `MapState & ScopeState &
    UiState` (Leaflet handles / scoping inputs / display prefs). `View` is `"destinations" |
    "plan"`. Plus `qs()` / `setStatus()` / the scope-change hook and small formatters.
  - `src/prefs.ts` - the `localStorage`-persisted prefs (theme / units / text-size / months).
    Selected genera are server-side instead (`app_genera` via `/api/genera/{taxon_id}`).
  - `src/views/views.ts` - the ranked-list flow. `runDestinations()` fetches `/api/destinations`
    and, when `state.sort === "active"`, delegates to `runActiveNow()` (the old "Fruiting now"
    tab: fetches `/api/alerts`, same card shell). `buildResultCard` (in `ui/card-dom.ts`) is the
    one card template both branches render through; `addCard()` plots the marker + wires
    select/Details/shortlist; `collapsibleRankList()` keeps the top 3 as hero cards and collapses
    the rest behind "Show N more regions". `sort.ts` holds the `Sort` labels/order.
  - `src/views/details.ts` - the Details view (per-region Calendar / Photos / Trails /
    Campgrounds tabs, full ARIA tab pattern), swapped into `#panel` from a card's "Details"
    button; a back button restores the list from cache. `destination-tabs.ts` holds the four tab
    loaders (`createLazyLoader`-guarded).
  - `src/views/shortlist.ts` - the route shortlist. "+ Plan" on a card adds a region id; the
    `#route-bar` at the panel's foot ("Plan a road trip" / "N spots picked") enters Plan mode,
    and `views/plan.ts`'s `runPlan()` sends the picks as `/api/plan?waypoints=`.
  - `src/views/plan.ts` - the route-planning UI (form + `TripPlan` render + GPX/JSON export).
    `src/views/view-run.ts` - `refreshCurrentView` / `rerenderCurrentView`, the single
    "re-run the open panel" owner (now just `plan` vs. everything-else).
  - `src/ui/why.ts` - `whySentence`: the plain-language line leading each card, synthesised from
    the `/api/destinations` payload alone (top genus + its `pheno_trend` phrase, in-window record
    count, recent-rain state, a burn-scar / active-fire clause when relevant) - no extra fetch.
  - `src/ui/pills.ts` - the filter-pill row: **Sort / Radius / Months / Genera / Layers** (5
    pills; issue #301 folded the 8 old land/camp/fire/aerial toggles into one "Layers" popover).
    `src/ui/pill.ts` is the popover primitive. `src/ui/ui-prefs.ts` wires the search-bar `⋮`
    menu (units / theme / text-size). `src/ui/card-select.ts` / `card-dom.ts` / `lazy-panel.ts`
    / `autocomplete.ts` - shared UI primitives.
  - `src/map/map.ts` - Leaflet init, theme/tile switching, `markerPalette()` (reads `tokens.css`
    at runtime, memoised per theme), the marker hierarchy in `plot()` (top-3 = filled circle +
    rank numeral, next-7 = ring, 11+ = dim moss dot), `selectSize`/`deselectSize`, `clear*()`,
    and the opt-in aerial overlay (`setAerialEnabled` / `showSatelliteOverlay`). The base layer
    is a Protomaps PMTiles vector map rendered by MapLibre GL (`src/map/basemap.ts`, with the
    `basemap-theme.ts` contrast pass and `basemap-roads.ts` forest-road/trail styling on top;
    code-split, see docs/data-sources.md), real light/dark cartography that swaps on theme
    change; no raster fallback - the server must send a `basemap_url` (`FORAY_BASEMAP_URL`) or
    the map has overlays but no base.
    `src/map/layers.ts` (camps/land/fire/precise fetch + render), `src/map/sheet.ts`
    (mobile bottom sheet), `popup.ts` / `markers.ts` / `layer-lifecycle.ts` (primitives).
  - `src/api/` - the typed client (`openapi-fetch`, `client.ts`: `getJson` / `postJson` /
    `deleteJson` throw `ApiError` on non-2xx, `openRefreshStream` is the typed SSE reader) +
    `schema.ts` generated from the backend OpenAPI via `openapi-typescript`. `npm run gen:api`
    regenerates it; CI fails on a diff, so it never drifts. Every call goes through `client.ts` -
    no raw `fetch` (place-search autocomplete is `GET /api/location/search`, proxied server-side,
    issue #145).
  - `src/refresh.ts` (SSE refresh + set-location), `src/main.ts` (DOM wiring/orchestration).

  Vitest covers the pure helpers + the DOM builders (`*.test.ts` beside the source); `npm test`
  runs in `just frontend`. `GET /api/coverage` exists on the backend but has no frontend consumer
  yet. Builds into `../src/foray/web/dist`. Theme is `data-theme`-driven with a `localStorage`
  preference (default **dark**), set before first paint by `public/theme-init.js` (external, so
  the CSP can stay `script-src 'self'`).

  **Aerial imagery** is opt-in now (the Layers pill's "Aerial imagery" toggle; it was
  auto-on-select before #301). When on, selecting a destination fills its true footprint with an
  Esri World Imagery raster plus a matching roads/labels overlay (`showSatelliteOverlay`, its own
  Leaflet pane between the tile and vector panes so the ring/markers/trails still draw on top,
  CSS-clipped to a circle). The frontend requests
  `/api/destinations/{region_id}/satellite/{image,labels}`; the backend (`sources/satellite.py`,
  `region_satellite` table, `foray backfill-satellite`) stitches both rasters from Esri's real
  XYZ tile pyramids, not the dynamic `MapServer/export` renderer - see docs/data-sources.md and
  the **Map imagery** convention below. Fetched once per region, never re-requested on zoom.

## Conventions

Follows the global `python` skill: uv, ruff, ty, pytest, and **no single-letter variable
names**. Tests are hermetic - never hit the network (scoring uses fixtures, geocoding is
mocked).

No CORS middleware is configured, which is intentionally safe by omission (no
`Access-Control-Allow-Origin` = no cross-origin JS can read responses). Don't add one later
without scoping `allow_origins` to the real domain.

**Map imagery: prefer the provider's tile pyramid over a custom/dynamic render, always.** This is
the general rule, not an Esri-specific one - it applies to any future imagery/basemap-style
provider, not just ArcGIS. Every serious map provider (Esri, Mapbox, OSM, Google) actually
distributes imagery as a "tile pyramid": the world pre-rendered once at each zoom level, chopped
into a grid of small images addressed by `{z}/{x}/{y}`, pre-rendered and CDN-cached - this is
what `L.tileLayer` (the aerial overlay in `layers.ts`) and the PMTiles vector base are both
*built around*, and what every slippy map client expects. Most providers *also* expose a
"flexible" dynamic-render endpoint (Esri's
`MapServer/export`: give it any bbox/size, it renders an image on the spot) that looks like the
simpler integration - one call instead of tile-grid math - but isn't: it does real work per
request instead of handing back something already computed, so it's slow (25-45s per call for
Esri's, measured, issue #293), unreliable under load (~95% failure rate at just 6 concurrent
requests), and can have quality quirks a tile pyramid doesn't (Esri's label layer draws text at a
fixed pixel height regardless of requested resolution, so a big sharp export makes text
illegibly tiny - a tile pyramid's labels are correctly sized per zoom by construction, the same
as every other slippy map). Before integrating a new imagery source, check whether it publishes a
tile pyramid (for ArcGIS: `.../MapServer?f=json` -> `"capabilities"` says `"Map,Tilemap"`) and
reach for that first. `sources/satellite.py` is the reference implementation: compute the tile
range covering your target bbox at a chosen zoom, fetch tiles concurrently, stitch + crop with
Pillow to the exact bbox (since an arbitrary bbox won't land on tile boundaries). This doesn't
apply to *vector*/attribute data - `sources/land.py`/`sources/fire.py`/`sources/trails.py`'s
ArcGIS `query`/`FeatureServer` endpoints return features, not pixels, and are the right tool for
that regardless of what imagery capabilities the same server might also expose.

## Commands

All common operations are centralized in the **justfile** (`uv tool install rust-just`). It
exports PG* env vars and prepends the nvm Node path automatically. `just` with no arguments
lists every recipe grouped by area; deploy recipes live in the `ansible` module (`just ansible`).

### Quick start

```bash
just install            # uv sync + frontend npm ci
just db                 # start Postgres
just ingest             # one-shot all-regions ingest + phenology rebuild
just start              # http://localhost:8000 (app + postgres)
just scheduler          # optional: start the background ingest/refresh loop
```

### just recipes

| Recipe | What it does |
|---|---|
| `just db` | Start Postgres (docker compose), wait for ready |
| `just install` | `uv sync` + `cd frontend && npm ci` |
| `just lint` | `ruff format --check` + `ruff check` + `ty check` + `vulture` |
| `just test` | Start Postgres if needed, then `pytest` |
| `just check` | `lint` + `test` (the full local CI gate) |
| `just frontend` | Build the Vite/TypeScript client bundle |
| `just start` | Build + start app + postgres |
| `just scheduler` | Start the background scheduler (observation + layer refresh loops) |
| `just stop` | Stop all containers (including scheduler if running) |
| `just ingest` | One-shot all-regions ingest |
| `just clean` | Tear down containers + volumes |
| `just ansible deploy` | Deploy to the droplet (see `just ansible` for the rest) |

### Backend CLI

```bash
uv run foray ingest --all-regions  # pull observations for all coverage regions
uv run foray refresh               # ingest + rebuild phenology/regions (all layers)
uv run foray serve --host 0.0.0.0 --port 8000
```

### Frontend dev (hot-reload)

```bash
# Terminal 1 - backend
just db && uv run foray serve

# Terminal 2 - frontend (Vite on :5173, proxies /api/* to uvicorn on :8000)
cd frontend && npm run dev
```

Rerun `npm run gen:api` (from `frontend/`) after changing any `/api/*` route.

### Tests

Run one test file / one test / by keyword:

```bash
uv run pytest tests/test_scoring.py
uv run pytest tests/test_scoring.py::test_april_ranks_morel_region_first
uv run pytest -k haversine
```

### Gate before finishing

```bash
just check
```

When touching `frontend/`, also run:

```bash
just frontend
```

## Data model notes

- Only `quality_grade=research` counts toward scoring.
- Regions are uniform lat/lng grid cells (`cell_deg`), id = `"{ilat}_{ilng}"`, derived in
  SQL - never stored redundantly. Change `cell_deg` -> re-run `foray refresh`.
- Location is per-area: changing it (UI `POST /api/location`) immediately runs scoring against
  cached data. The saved override (`app_location` table in Postgres) wins over the env var
  defaults and survives restarts.
- The Postgres database is fully rebuildable via `foray refresh` - with one exception:
  `app_location` (per-device "Set location" override, `src/foray/cache.py`) is genuinely
  user-authored state that no ingest path regenerates. In prod, it lives on the DigitalOcean
  managed Postgres cluster provisioned by `infra/ansible/tasks/provision/database.yml`
  (`digitalocean.cloud.database_cluster`), so it's covered by whatever automatic
  snapshot/backup policy that managed offering applies by default - this repo doesn't
  configure, verify, or restore-test that policy, so treat it as unconfirmed rather than a
  guarantee. There is no `pg_dump` cron or other app-level backup for it. Local/self-hosted
  `docker-compose` deployments have it even less covered: its only persistence is the
  `foray-postgres-data` named volume (`docker-compose.yml`), with no backup at all. Either
  way, if the underlying storage is lost, every visitor's saved location silently reverts to
  the env-var default home with no warning - see issue #114.
- Connection info comes from `PG*` env vars (see `src/foray/cache.py`).
- `campsites` (developed campgrounds) is keyed by `"{source}:{source_id}"` and upserted
  idempotently. Needs `RIDB_API_KEY` (gitignored `.env` locally; env var in prod) - absent,
  camps ingest is a no-op. `free` is nullable: TRUE only on an explicit no-fee signal, else
  NULL (unknown).
- `observations` includes `place_guess`, `uri`, and `obscured` columns enriched from iNat
  during ingest. Existing rows backfill via ON CONFLICT DO UPDATE with COALESCE on next ingest.
  `elevation_m` (issue #36) is filled separately, post-ingest, from Open-Meteo's DEM
  (`ingest.backfill_elevations` / `foray backfill-elevation`) - NULL until looked up.
  `precip_7d_mm` / `precip_30d_mm` (issue #226) are filled the same way from Open-Meteo's ERA5
  archive (`ingest.backfill_precip` / `foray backfill-precip`), snapping to the region grid and
  caching daily values in `precip_daily`; a window still inside ERA5's ~5-7 day lag stays NULL
  and is retried. Observations dated before ERA5 (1940) are excluded from the pending set - the
  archive 400s on them and one wedges the whole grid cell; per-cell fetches are span-capped and
  a per-cell 4xx skips that cell rather than stopping. The `precipitation` layer table holds
  recent rain per region cell (`ingest.refresh_precipitation` / `foray refresh-precip`, forecast
  API) - flushed in batches and skips cells refreshed in the last ~20h so a long / restarted run
  resumes. Informational only, no scoring - same posture as elevation.
- Target genera aren't configured in code - `foray genera-refresh` keeps the full catalog
  synced, `foray ingest` pulls every Fungi observation and resolves each one's own genus from
  its taxon ancestry, and users pick their targets in the search UI (per-device, `app_genera`).
- Droplet CPU/memory/disk alerting (issue #84 - a past ENOSPC incident went unnoticed until the
  app broke) is provisioned as code: `infra/ansible/tasks/provision/monitoring.yml` creates DO
  monitoring alert policies via `digitalocean.cloud.monitoring_alert_policy`, opt-in behind the
  `foray_alert_email` var (`FORAY_ALERT_EMAIL` env; unset skips creation). A Cloudflare health
  check is a further, complementary layer this repo doesn't automate - set one up manually in
  the Cloudflare dashboard (Traffic -> Health Checks) against `https://forayplanner.com/` if
  wanted; nothing here depends on it.

## Not in scope

This is a trip-planning and mapping tool. Make **no** identification, edibility, or safety
claims anywhere - no authored species descriptions, no toxicity/lookalike text. Any such
information is deferred to each taxon's iNaturalist page (`inatUrl()` in `state.ts`), which
the UI links. Keep it that way.
