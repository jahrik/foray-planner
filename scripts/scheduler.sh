#!/bin/sh
set -eu

OBS_INTERVAL="${FORAY_INGEST_INTERVAL_HOURS:-24}"
LAYERS_INTERVAL="${FORAY_LAYERS_INTERVAL_HOURS:-168}"
REVALIDATE_INTERVAL="${FORAY_REVALIDATE_INTERVAL_HOURS:-168}"
RESYNC_INTERVAL="${FORAY_RESYNC_INTERVAL_HOURS:-1}"
RESYNC_BATCH_SIZE="${FORAY_RESYNC_BATCH_SIZE:-2000}"
ELEVATION_INTERVAL="${FORAY_ELEVATION_INTERVAL_HOURS:-1}"
# Rain changes far faster than the 168h camps/land/trails layers, so it gets its own knob.
PRECIP_INTERVAL="${FORAY_PRECIP_INTERVAL_HOURS:-24}"
# Perimeter data updates ~daily; prod can drop this during fire season without a code change.
FIRE_INTERVAL="${FORAY_FIRE_INTERVAL_HOURS:-24}"
# High cap on purpose - each pass drains until Open-Meteo's free tier 429s it (lookup_batch
# backs off on Retry-After, then gives up), so this is really just an upper safety bound.
ELEVATION_LIMIT="${FORAY_ELEVATION_LIMIT:-20000}"

obs_last=0
layers_last=0
revalidate_last=0
resync_last=0
elevation_last=0
precip_last=0
fire_last=0

while true; do
  now=$(date +%s)

  if [ $((now - obs_last)) -ge $((OBS_INTERVAL * 3600)) ]; then
    echo "[scheduler] $(date -Iseconds) Starting observation ingest (all countries)…"
    foray ingest --countries && obs_last=$(date +%s) || echo "[scheduler] observation ingest failed"
  fi

  if [ $((now - layers_last)) -ge $((LAYERS_INTERVAL * 3600)) ]; then
    echo "[scheduler] $(date -Iseconds) Starting layers refresh (camps, dispersed: home radius; land, trails: all coverage)…"
    if foray refresh --with camps,dispersed && foray refresh --with land,trails --all; then
      layers_last=$(date +%s)
    else
      echo "[scheduler] layers refresh failed"
    fi
  fi

  # Cached observations only ever get re-checked within a narrow incremental overlap window
  # (ingest.py) - a handful of fungal genus names are homonyms of common animal genera (e.g.
  # Olla the fungus vs. the ladybug genus Olla), so misidentified non-fungal observations
  # accumulate over time and never self-correct without this (see ingest.revalidate).
  if [ $((now - revalidate_last)) -ge $((REVALIDATE_INTERVAL * 3600)) ]; then
    echo "[scheduler] $(date -Iseconds) Starting observation revalidation (cross-kingdom homonym check)…"
    foray revalidate && revalidate_last=$(date +%s) || echo "[scheduler] revalidation failed"
  fi

  # Slow whole-table grind (small batch, frequent interval) - the only path that eventually
  # re-verifies every column of every cached row, including `obscured` (NULL for the bulk
  # historical import) and misidentifications too rare within their genus for revalidate's
  # ratio check to catch (see ingest.resync).
  if [ $((now - resync_last)) -ge $((RESYNC_INTERVAL * 3600)) ]; then
    echo "[scheduler] $(date -Iseconds) Starting observation resync (batch of $RESYNC_BATCH_SIZE)…"
    foray resync --batch-size "$RESYNC_BATCH_SIZE" && resync_last=$(date +%s) || echo "[scheduler] resync failed"
  fi

  # Steady drain of the per-observation elevation backlog (issue #36). Open-Meteo's free DEM
  # rate-limits a burst hard, so each pass only enriches a few hundred rows regardless of the
  # limit; the daily ingest also tops up its own new rows. Rebuilds phenology when it enriched
  # anything so destination cards pick up the new region means.
  if [ $((now - elevation_last)) -ge $((ELEVATION_INTERVAL * 3600)) ]; then
    echo "[scheduler] $(date -Iseconds) Starting elevation backfill (limit $ELEVATION_LIMIT)…"
    foray backfill-elevation --limit "$ELEVATION_LIMIT" && elevation_last=$(date +%s) || echo "[scheduler] elevation backfill failed"
  fi

  # Rainfall (issue #226): drain the per-observation antecedent-rain backlog (ERA5 archive,
  # rebuilds phenology when it enriches anything) and refresh the recent-rain-per-destination
  # layer (forecast API). Both hit Open-Meteo's free tier, which 429s a burst - each pass does
  # what it can and the next tick resumes.
  if [ $((now - precip_last)) -ge $((PRECIP_INTERVAL * 3600)) ]; then
    echo "[scheduler] $(date -Iseconds) Starting rainfall backfill + recent-rain layer refresh…"
    if foray backfill-precip && foray refresh-precip; then
      precip_last=$(date +%s)
    else
      echo "[scheduler] rainfall refresh failed"
    fi
  fi

  # Wildfire (issue #227): active perimeters + points (replace semantics), 3+current years of
  # burn-scar history, MTBS severity. NIFC/MTBS ArcGIS; one source down is skipped, not fatal.
  if [ $((now - fire_last)) -ge $((FIRE_INTERVAL * 3600)) ]; then
    echo "[scheduler] $(date -Iseconds) Starting wildfire refresh (active + burn scars + MTBS)…"
    foray fire && fire_last=$(date +%s) || echo "[scheduler] wildfire refresh failed"
  fi

  sleep 300
done
