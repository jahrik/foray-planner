#!/usr/bin/env bash
# Provision (or update) the AWS Lightsail instance that runs foray-planner.
#
# Idempotent: safe to re-run. Each step checks whether its resource already exists
# before creating it, so re-running after a partial failure (or to pick up a bundle/AZ
# change) won't duplicate instances, disks, or static IPs.
#
# Prerequisites:
#   - AWS CLI v2, configured (`aws configure` or SSO) with Lightsail permissions.
#   - `jq` for parsing CLI JSON output.
#
# What this does NOT do (deliberately - see docs/deployment.md):
#   - Set RIDB_API_KEY (SSH in after first boot and edit /etc/foray/env by hand).
#   - Cloudflare DNS/Access/domain setup (separate manual checklist, docs/deployment.md).
#   - Install Postgres or migrate off DuckDB (single-box v1 only, see TODO.md Epic 6).

set -euo pipefail

# --- Config - override via environment before running, e.g. `REGION=us-east-1 ./deploy.sh` ---
INSTANCE_NAME="${INSTANCE_NAME:-foray-planner}"
REGION="${REGION:-us-west-2}"
AVAILABILITY_ZONE="${AVAILABILITY_ZONE:-${REGION}a}"
# micro_3_0 = 1 GB RAM / 2 vCPU / 40 GB SSD boot disk, ~$7/mo. Bump to small_3_0 (2 GB,
# ~$12/mo) if refresh over a wide radius is slow/OOMs. Confirm current ids + prices with:
#   aws lightsail get-bundles --query "bundles[?contains(instanceType, 'linux')].bundleId"
BUNDLE_ID="${BUNDLE_ID:-micro_3_0}"
# Confirm current blueprint ids with: aws lightsail get-blueprints --query "blueprints[?platform=='LINUX_UNIX'].blueprintId"
BLUEPRINT_ID="${BLUEPRINT_ID:-ubuntu_24_04}"
DISK_NAME="${DISK_NAME:-${INSTANCE_NAME}-data}"
DISK_SIZE_GB="${DISK_SIZE_GB:-16}"
STATIC_IP_NAME="${STATIC_IP_NAME:-${INSTANCE_NAME}-ip}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for bin in aws jq; do
	command -v "$bin" >/dev/null || {
		echo "missing required tool: $bin" >&2
		exit 1
	}
done

echo "== Region ${REGION}, AZ ${AVAILABILITY_ZONE}, bundle ${BUNDLE_ID}, blueprint ${BLUEPRINT_ID} =="

instance_exists() {
	aws lightsail get-instance --region "$REGION" --instance-name "$INSTANCE_NAME" >/dev/null 2>&1
}

if instance_exists; then
	echo "== Instance '$INSTANCE_NAME' already exists, skipping create =="
else
	echo "== Creating instance '$INSTANCE_NAME' =="
	aws lightsail create-instances \
		--region "$REGION" \
		--instance-names "$INSTANCE_NAME" \
		--availability-zone "$AVAILABILITY_ZONE" \
		--blueprint-id "$BLUEPRINT_ID" \
		--bundle-id "$BUNDLE_ID" \
		--user-data "file://${SCRIPT_DIR}/cloud-init.yaml" \
		>/dev/null
	echo "   waiting for it to come up..."
	until [ "$(aws lightsail get-instance --region "$REGION" --instance-name "$INSTANCE_NAME" | jq -r .instance.state.name)" = "running" ]; do
		sleep 5
	done
fi

echo "== Firewall: opening SSH (22) + HTTP (80) =="
aws lightsail put-instance-public-ports \
	--region "$REGION" \
	--instance-name "$INSTANCE_NAME" \
	--port-infos fromPort=22,toPort=22,protocol=TCP fromPort=80,toPort=80,protocol=TCP \
	>/dev/null

if aws lightsail get-static-ip --region "$REGION" --static-ip-name "$STATIC_IP_NAME" >/dev/null 2>&1; then
	echo "== Static IP '$STATIC_IP_NAME' already exists =="
else
	echo "== Allocating static IP '$STATIC_IP_NAME' =="
	aws lightsail allocate-static-ip --region "$REGION" --static-ip-name "$STATIC_IP_NAME" >/dev/null
fi
aws lightsail attach-static-ip \
	--region "$REGION" \
	--static-ip-name "$STATIC_IP_NAME" \
	--instance-name "$INSTANCE_NAME" \
	>/dev/null

if aws lightsail get-disk --region "$REGION" --disk-name "$DISK_NAME" >/dev/null 2>&1; then
	echo "== Disk '$DISK_NAME' already exists =="
else
	echo "== Creating ${DISK_SIZE_GB}GB disk '$DISK_NAME' =="
	aws lightsail create-disk \
		--region "$REGION" \
		--disk-name "$DISK_NAME" \
		--availability-zone "$AVAILABILITY_ZONE" \
		--size-in-gb "$DISK_SIZE_GB" \
		>/dev/null
	until [ "$(aws lightsail get-disk --region "$REGION" --disk-name "$DISK_NAME" | jq -r .disk.state)" = "available" ]; do
		sleep 5
	done
fi

disk_state="$(aws lightsail get-disk --region "$REGION" --disk-name "$DISK_NAME" | jq -r .disk.state)"
if [ "$disk_state" = "in-use" ]; then
	echo "== Disk '$DISK_NAME' already attached =="
else
	echo "== Attaching disk '$DISK_NAME' as /dev/xvdf (matches cloud-init.yaml's mount) =="
	aws lightsail attach-disk \
		--region "$REGION" \
		--disk-name "$DISK_NAME" \
		--instance-name "$INSTANCE_NAME" \
		--disk-path /dev/xvdf \
		>/dev/null
fi

ip="$(aws lightsail get-static-ip --region "$REGION" --static-ip-name "$STATIC_IP_NAME" | jq -r .staticIp.ipAddress)"

cat <<EOF

== Done ==
Static IP: ${ip}

Next steps (see docs/deployment.md for the full checklist):
  1. Wait ~1-2 min for cloud-init to finish (installs Docker, partitions + mounts the disk,
     enables the systemd unit but does NOT start it yet).
  2. SSH in and set the real API key:
       ssh ubuntu@${ip}
       sudo nano /etc/foray/env      # uncomment + fill in RIDB_API_KEY=...
       sudo systemctl start foray-planner
       sudo systemctl status foray-planner
  3. Confirm it's serving:
       curl http://${ip}/api/config
  4. Point Cloudflare DNS at ${ip} (proxied) and set up Access - see docs/deployment.md.
EOF
