#!/usr/bin/env bash
# Point a Cloudflare-managed domain at the Lightsail static IP (proxied, so Cloudflare
# terminates TLS and Access sits in front). Optional convenience wrapper around the
# Cloudflare API - the DNS record can equally be created by hand in the dashboard.
#
# Requires:
#   CF_API_TOKEN   - a token scoped to Zone:DNS:Edit for the target zone
#   CF_ZONE_ID     - the zone id for your domain (Cloudflare dashboard -> domain -> Overview)
#   RECORD_NAME    - e.g. "foray" for foray.example.com, or "@" for the bare domain
#   TARGET_IP      - the Lightsail static IP (deploy.sh prints this)
#
# Usage:
#   CF_API_TOKEN=... CF_ZONE_ID=... RECORD_NAME=foray TARGET_IP=1.2.3.4 ./cloudflare-dns.sh

set -euo pipefail

: "${CF_API_TOKEN:?set CF_API_TOKEN}"
: "${CF_ZONE_ID:?set CF_ZONE_ID}"
: "${RECORD_NAME:?set RECORD_NAME}"
: "${TARGET_IP:?set TARGET_IP}"

command -v jq >/dev/null || {
  echo "missing required tool: jq" >&2
  exit 1
}

api() {
  curl -sS -X "$1" "https://api.cloudflare.com/client/v4/$2" \
    -H "Authorization: Bearer ${CF_API_TOKEN}" \
    -H "Content-Type: application/json" \
    "${@:3}"
}

existing_id="$(
  api GET "zones/${CF_ZONE_ID}/dns_records?type=A&name=${RECORD_NAME}.$(api GET "zones/${CF_ZONE_ID}" | jq -r .result.name)" |
    jq -r '.result[0].id // empty'
)"

payload="$(jq -n --arg name "$RECORD_NAME" --arg ip "$TARGET_IP" \
  '{type:"A", name:$name, content:$ip, ttl:1, proxied:true}')"

if [ -n "$existing_id" ]; then
  echo "== Updating existing A record ($existing_id) =="
  api PUT "zones/${CF_ZONE_ID}/dns_records/${existing_id}" -d "$payload" | jq -r '.success, .errors'
else
  echo "== Creating A record =="
  api POST "zones/${CF_ZONE_ID}/dns_records" -d "$payload" | jq -r '.success, .errors'
fi

echo "== Done. Remaining manual steps in Cloudflare dashboard: =="
echo "  - SSL/TLS mode: Flexible (origin serves plain HTTP on :80) or Full if you add TLS on the box."
echo "  - Zero Trust -> Access -> Applications: add an app for this hostname, policy = your email/Google."
