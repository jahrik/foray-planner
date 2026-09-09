#!/bin/sh
# Build the vector basemap archive for foray-planner.
#
# Slices a US-only extract out of Protomaps' daily planet PMTiles build and (optionally)
# uploads it to the DO Spaces bucket the frontend reads via cfg.basemap_url. The extract is
# ~15-40 GB - far bigger than the droplet's root disk - so run this on a workstation or a
# throwaway large droplet, not the app host.
#
# Requirements:
#   - pmtiles CLI          https://github.com/protomaps/go-pmtiles/releases
#   - s3cmd or aws CLI      only for the upload step
#
# Usage:
#   scripts/build_basemap_pmtiles.sh [OUTFILE]
#
# Env:
#   PMTILES_SOURCE   planet archive to slice. Default: Protomaps' current hosted build
#                    (https://build.protomaps.com/YYYYMMDD.pmtiles - `pmtiles` reads it over
#                    HTTP range requests, no full download).
#   BBOX             min_lon,min_lat,max_lon,max_lat. Default: CONUS + a margin.
#   UPLOAD_TARGET    e.g. s3://foray-basemap/us.pmtiles - when set, upload after building.
set -eu

OUTFILE="${1:-us.pmtiles}"
BBOX="${BBOX:--125.1,24.3,-66.7,49.5}"
DEFAULT_SOURCE="https://build.protomaps.com/$(date -u +%Y%m%d).pmtiles"
PMTILES_SOURCE="${PMTILES_SOURCE:-$DEFAULT_SOURCE}"

command -v pmtiles >/dev/null 2>&1 || {
  echo "error: pmtiles CLI not found - https://github.com/protomaps/go-pmtiles/releases" >&2
  exit 1
}

echo "source : $PMTILES_SOURCE"
echo "bbox   : $BBOX"
echo "out    : $OUTFILE"

# `extract` pulls only the tiles inside the bbox from the (remote) planet archive and writes a
# standalone, servable PMTiles file. --maxzoom is left at the source's (15) so labels stay
# crisp; drop it to 13-14 here if the file size needs to come down.
pmtiles extract "$PMTILES_SOURCE" "$OUTFILE" --bbox="$BBOX"

echo "built  : $OUTFILE ($(du -h "$OUTFILE" | cut -f1))"
pmtiles show "$OUTFILE" | sed -n '1,12p'

if [ -n "${UPLOAD_TARGET:-}" ]; then
  echo "upload : $UPLOAD_TARGET"
  if command -v s3cmd >/dev/null 2>&1; then
    # public-read + a long cache TTL: the archive is immutable per build, the frontend
    # points at a fixed key, and range requests must not be rewritten.
    s3cmd put "$OUTFILE" "$UPLOAD_TARGET" --acl-public --no-preserve \
      --add-header="Cache-Control: public, max-age=604800, immutable"
  elif command -v aws >/dev/null 2>&1; then
    aws s3 cp "$OUTFILE" "$UPLOAD_TARGET" --acl public-read \
      --cache-control "public, max-age=604800, immutable"
  else
    echo "error: UPLOAD_TARGET set but neither s3cmd nor aws CLI is installed" >&2
    exit 1
  fi
  echo "done. Set FORAY_BASEMAP_URL to the CDN URL for this object."
fi
