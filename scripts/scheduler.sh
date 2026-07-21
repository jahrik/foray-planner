#!/bin/sh
set -eu

OBS_INTERVAL="${FORAY_INGEST_INTERVAL_HOURS:-24}"
LAYERS_INTERVAL="${FORAY_LAYERS_INTERVAL_HOURS:-168}"
REVALIDATE_INTERVAL="${FORAY_REVALIDATE_INTERVAL_HOURS:-168}"

obs_last=0
layers_last=0
revalidate_last=0

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

  sleep 300
done
