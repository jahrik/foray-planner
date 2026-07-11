# 🍄 Foray Planner

[![CI](https://github.com/jahrik/foray-planner/actions/workflows/ci.yml/badge.svg)](https://github.com/jahrik/foray-planner/actions/workflows/ci.yml)
[![CD](https://github.com/jahrik/foray-planner/actions/workflows/cd.yml/badge.svg)](https://github.com/jahrik/foray-planner/actions/workflows/cd.yml)

Plan where to go mushroom hunting next, using [iNaturalist](https://www.inaturalist.org)
observation data. It cross-references *what has been observed in a given month* against
*where it has historically been observed* to rank travel destinations — and inverts the same
data to show the best months for a place, or which target species are **being observed right
now**.

This is a trip-planning and mapping tool only. It makes no identification, edibility, or
safety claims of any kind; each species links to its iNaturalist page for that information.

## How it works

1. **Ingest** — pull research-grade observations of a curated set of target fungi
   (`data/species_seed.yaml`) within a home-base radius, into a local DuckDB cache.
2. **Phenology** — bin observations into a lat/lng grid and build per-(species, region,
   month) counts: the seasonality curve.
3. **Score** — three modes from the same primitives:
   - **Destinations**: pick month(s) → rank regions by observation activity. Defaults to the
     current calendar month.
   - **Calendar**: click a region → its busiest months (12-bucket heatmap).
   - **Observed now**: recent (trailing-weeks) observations of target species by region.

All iNat access is throttled, retried on transient network errors, and cached; queries run
against the local DuckDB, so the web app is fast and offline once data is pulled.

## Quick start

```bash
uv sync
uv run foray refresh      # pull data + build phenology (first run hits iNat; minutes)
uv run foray serve        # http://127.0.0.1:8000
```

Then open the app and set your location from the header bar — no config editing needed.

## Using the web app

- **Set location** — type a place name (`Coos Bay, OR`) or raw `lat,lng` in the header bar.
  The name is geocoded via OpenStreetMap; the choice is saved to `data/location.json` (so it
  survives restarts) and the app automatically re-fetches iNaturalist data for the new area.
- **Months** default to the current month; toggle any combination.
- **Map markers** — magenta scales with historical strength, cyan marks fresh/recent
  activity, and the white dot is your location. Each species chip links to its iNat page.
- **Refresh data** re-pulls from iNaturalist for the current location (runs in the
  background with a progress indicator).

## CLI

```bash
uv run foray ingest       # pull observations only
uv run foray refresh      # ingest + rebuild phenology/regions tables
uv run foray serve --host 0.0.0.0 --port 8000
```

## Configuration

`config.yaml` — default home base (lat/lng/radius), grid `cell_deg`, ingest window
(`since_year`, `quality_grade`), and the `recent_weeks` window for the live signal. Values
are range-validated on load (pydantic), so a bad edit fails with a clear message.

`data/location.json` — the active location set from the UI; overrides `config.yaml`'s home
so the defaults file stays pristine. Delete it to fall back to the config default.

`data/species_seed.yaml` — the curated target taxa, each mapped to its iNat `taxon_id`.
No authored descriptions; the UI links each taxon to iNaturalist. Add or remove species
here, then re-run `foray refresh`.

## Development

```bash
uv run ruff format . && uv run ruff check .
uv run ty check
uv run pytest
```

Tests are hermetic (no network): scoring runs on hand-built fixtures and geocoding is mocked.
Conventions follow the `python` skill — including no single-letter variable names.

## Data & attribution

Observation data © iNaturalist and its contributors; geocoding © OpenStreetMap contributors.
Requests are throttled (~1/s via pyinaturalist) and sent with a descriptive User-Agent.
Respect the [iNaturalist API terms](https://www.inaturalist.org/pages/api+reference) and the
[Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/).

## License

[MIT](LICENSE) © Wesley Gill
