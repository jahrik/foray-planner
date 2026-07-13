#!/bin/sh
set -eu

OBS_INTERVAL="${FORAY_INGEST_INTERVAL_HOURS:-24}"
LAYERS_INTERVAL="${FORAY_LAYERS_INTERVAL_HOURS:-168}"

obs_last=0
layers_last=0

while true; do
    now=$(date +%s)

    if [ $((now - obs_last)) -ge $((OBS_INTERVAL * 3600)) ]; then
        echo "[scheduler] $(date -Iseconds) Starting observation ingest (all regions)…"
        foray ingest --all-regions && obs_last=$(date +%s) || echo "[scheduler] observation ingest failed"
    fi

    if [ $((now - layers_last)) -ge $((LAYERS_INTERVAL * 3600)) ]; then
        echo "[scheduler] $(date -Iseconds) Starting layers refresh (camps, land, dispersed, trails)…"
        foray refresh --with camps,land,dispersed,trails && layers_last=$(date +%s) || echo "[scheduler] layers refresh failed"
    fi

    sleep 300
done
