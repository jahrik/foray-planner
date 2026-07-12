# AGENTS.md — Foray Planner

Python web app that ranks mushroom-hunting destinations from iNaturalist observation
phenology. Local-only project (no GitHub remote yet).

## Layout

- `src/foray/config.py` — pydantic models (`Config`, `Home`, `Species`) with range
  validation; loads `config.yaml`, applies the `location.json` override, saves locations.
  Config is the file-boundary trust layer — internal scoring types stay dataclasses.
- `src/foray/inat.py` — throttled pyinaturalist wrapper (observations, species_counts,
  monthly histogram). Descriptive User-Agent; deep-paginates via `id_above`; `_with_retries`
  backs off on transient network errors so one blip doesn't abort a long ingest.
- `src/foray/geocode.py` — resolve a place name (OpenStreetMap Nominatim) or raw `lat,lng`
  to coordinates. Network-mocked in tests.
- `src/foray/cache.py` — DuckDB schema + idempotent upserts (`ON CONFLICT`), ingest log.
- `src/foray/ingest.py` — pulls per seed taxon within the home radius; tags each obs with
  the **seed** taxon_id (not leaf species) so phenology is per foraging target.
- `src/foray/camps.py` — developed-campground ingest from the Recreation.gov **RIDB API**
  (httpx, key from env `RIDB_API_KEY`). Tiles the home radius into ≤50-mi query circles,
  dedupes facilities, clips to the true radius with `haversine_km`. Skipped (no-op) when the
  key is unset, so the iNat refresh still works. `free` is only asserted on an explicit
  no-fee signal — never guessed.
- `src/foray/scoring.py` — `build_phenology` (materializes `regions` + `phenology`) and the
  scoring modes: `rank_destinations`, `place_calendar`, `alerts`, and `camps_near` (campsites
  near a point, free-first by distance). Grid binning is one reusable SQL fragment (`_BINNED`).
- `src/foray/api.py` — FastAPI: `/api/{config,species,destinations,calendar,alerts,camps,
  location,refresh}` + `/` (serves the built client). `/api/camps` takes a `region_id` or a
  `lat`/`lng` plus `radius_km` + `free_only`. One shared DuckDB connection handing out
  per-request cursors; live config is mutable app state; `refresh` runs in a background thread
  with reads guarded while it rebuilds. `destinations` defaults to the current month when none
  is given.
- `src/foray/cli.py` — `foray ingest | camps | refresh | serve | openapi` (`camps` ingests
  campgrounds; `refresh` does obs + camps + phenology; `openapi` dumps the schema that feeds
  the frontend type generator).
- `src/foray/web/dist/` — the built client bundle (gitignored; emitted by the frontend build
  and served by FastAPI as static assets at `/assets` + `/`).
- `frontend/` — the web client: **Vite + TypeScript (strict)**, Leaflet map. `src/main.ts`
  (ported from the old `app.js`), `src/api/` (typed client + `schema.ts` generated from the
  backend's OpenAPI via `openapi-typescript`), `src/style.css`. Builds into
  `../src/foray/web/dist`. Marker palette is deliberately non-green (magenta = strength,
  cyan = recent) so it reads against the OSM terrain.

## Conventions

Follows the global `python` skill: uv, ruff, ty, pytest, and **no single-letter variable
names**. Tests are hermetic — never hit the network (scoring uses fixtures, geocoding is
mocked).

## Commands

```bash
uv sync                 # install deps into the venv
uv run foray refresh    # ingest iNat obs + build phenology (first run hits the network)
uv run foray serve      # http://127.0.0.1:8000  (--host / --port to override)
```

Run one test file / one test / by keyword:

```bash
uv run pytest tests/test_scoring.py
uv run pytest tests/test_scoring.py::test_april_ranks_morel_region_first
uv run pytest -k haversine
```

Gate before finishing:

```bash
uv run ruff format . && uv run ruff check . && uv run ty check && uv run pytest
```

When touching `frontend/`, also gate the client (Node ≥ 22; `npm ci` first):

```bash
cd frontend && npm run build   # tsc --noEmit + vite build
```

## Data model notes

- Only `quality_grade=research` counts toward scoring.
- Regions are uniform lat/lng grid cells (`cell_deg`), id = `"{ilat}_{ilng}"`, derived in
  SQL — never stored redundantly. Change `cell_deg` → re-run `foray refresh`.
- Location is per-area: changing it (UI `POST /api/location` or editing `location.json`)
  requires a `refresh` to fetch iNat data for the new radius. `location.json` overrides
  `config.yaml`'s home.
- The cache DB (`data/foray.duckdb`) is gitignored and fully rebuildable via `foray refresh`.
- `campsites` (developed campgrounds) is keyed by `"{source}:{source_id}"` and upserted
  idempotently. Needs `RIDB_API_KEY` (gitignored `.env` locally; env var in prod) — absent,
  camps ingest is a no-op. `free` is nullable: TRUE only on an explicit no-fee signal, else
  NULL (unknown). No legality/claims — surface ownership + link the source, never assert.
- Adding species: edit `data/species_seed.yaml` (resolve taxon_ids via `get_taxa`) then
  refresh.

## Not in scope

This is a trip-planning and mapping tool. Make **no** identification, edibility, or safety
claims anywhere — no authored species descriptions, no toxicity/lookalike text. Any such
information is deferred to each taxon's iNaturalist page (`Species.inat_url`), which the UI
links. Keep it that way.
