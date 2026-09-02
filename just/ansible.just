# Ansible (Digital Ocean deployment). Run as `just ansible <recipe>`.
# Required env vars for provisioning: DO_API_TOKEN, FORAY_SSH_KEY_NAME, FORAY_SSH_ALLOWED_IPS
# Required env vars for deploy-only: FORAY_DROPLET_IP (+ DB creds in /opt/foray-planner/foray.env)

# `justfile_directory()` resolves to the *root* justfile's dir (repo root), not this module's.
ansible_dir := justfile_directory() / "infra/ansible"

# List available recipes.
default:
    @just --list ansible

# uv sync + install the Galaxy collections from requirements.yml.
[group('deploy')]
install:
    cd {{ ansible_dir }} && uv sync
    cd {{ ansible_dir }} && uv run ansible-galaxy collection install -r requirements.yml

# yamllint + ansible-lint.
[group('deploy')]
lint:
    cd {{ ansible_dir }} && uv run yamllint .
    cd {{ ansible_dir }} && uv run ansible-lint

# Provision a fresh droplet (needs DO_API_TOKEN, FORAY_SSH_KEY_NAME, FORAY_SSH_ALLOWED_IPS).
[group('deploy')]
provision:
    @test -n "${DO_API_TOKEN:-}" || (echo "ERROR: DO_API_TOKEN not set" && exit 1)
    @test -n "${FORAY_SSH_KEY_NAME:-}" || (echo "ERROR: FORAY_SSH_KEY_NAME not set" && exit 1)
    @test -n "${FORAY_SSH_ALLOWED_IPS:-}" || (echo "ERROR: FORAY_SSH_ALLOWED_IPS not set" && exit 1)
    cd {{ ansible_dir }} && uv run ansible-playbook site.yml --tags foray:provision \
        -e foray_do_ssh_key_name=$FORAY_SSH_KEY_NAME \
        -e '{"foray_ssh_allowed_ips": ["'"$FORAY_SSH_ALLOWED_IPS"'"]}'

# Shared runner for the tag-driven, droplet-IP playbooks below (needs FORAY_DROPLET_IP).
[private]
_playbook tag *extra:
    @test -n "${FORAY_DROPLET_IP:-}" || (echo "ERROR: FORAY_DROPLET_IP not set" && exit 1)
    cd {{ ansible_dir }} && uv run ansible-playbook site.yml --tags foray:{{ tag }} \
        -i inventory/hosts.yml \
        -e foray_droplet_ip=$FORAY_DROPLET_IP {{ extra }}

# Deploy the current build to the droplet (needs FORAY_DROPLET_IP).
[group('deploy')]
deploy: (_playbook "deploy")

# Applies/updates the recurring cron jobs (ingest, layers, genera, revalidate, resync) - not
# part of `deploy` itself, matches how the existing cron jobs were originally set up.
[doc('Apply/update the recurring cron jobs on the droplet')]
[group('deploy')]
cron: (_playbook "cron")

# Manual/opt-in only - the foray-genera cron job already keeps the catalog fresh on a schedule.
# Use this to warm fungi_genera immediately on a fresh droplet (depends on the env file that
# `deploy` renders). Run this *before* `ingest-once` - ingest fails fast if the catalog is empty.
[doc('Warm fungi_genera on a fresh droplet now instead of waiting for cron')]
[group('deploy-once')]
genera-once: (_playbook "genera-once")

# Manual/opt-in only - the foray-ingest cron job already keeps data fresh on a schedule. Use
# this to warm data immediately after the *first* `deploy` on a fresh droplet (depends on the
# env file that `deploy` renders), instead of waiting for the next cron run.
[doc('Warm observation data on a fresh droplet now instead of waiting for cron')]
[group('deploy-once')]
ingest-once: (_playbook "ingest-once")

# One-off: push the already-filtered, already-verified data/inat_us_observations.jsonl (see
# `just bulk-filter`) to the droplet and load it - the fast path for issue #79 Phase 3b instead
# of waiting on the nightly ingest cron. Run `just ansible genera-once` first if the catalog
# hasn't been warmed yet.
[doc('Push the filtered bulk JSONL to the droplet and load it')]
[group('deploy-once')]
bulk-load-once: (_playbook "bulk-load-once")

# One-time: clear the whole per-observation elevation backlog by sampling local Copernicus
# GLO-90 tiles on the droplet, instead of trickling through Open-Meteo's ~10k/day free tier for
# ~200 days. Downloads ~5GB of tiles to the droplet, samples, then removes them.
[doc('One-pass elevation backfill from Copernicus GLO-90 tiles on the droplet')]
[group('deploy-once')]
backfill-elevation-dem-once: (_playbook "backfill-elevation-dem-once")

# One-time, slow (~3h against prod's current table size) full resync catch-up (see TODO.md) -
# the real live-recheck fix, not just the heuristic. Run it and don't block on it.
[doc('One-time full resync catch-up on the droplet (~3h, run detached)')]
[group('deploy-once')]
resync-once: (_playbook "resync-once")
