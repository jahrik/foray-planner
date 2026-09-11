# AGENTS.md - foray-planner Ansible deployment

## Purpose

Deploy foray-planner to Digital Ocean: managed Postgres cluster + Docker Droplet + cron-based data refresh.

## Key Variables

| Variable | Description |
|---|---|
| `foray_do_token` | DO API token (from `DO_API_TOKEN` env) |
| `foray_do_ssh_key_name` | SSH key name registered in DO |
| `foray_do_region` | DO region (default: sfo3) |
| `foray_app_image` | Container image (default: ghcr.io/jahrik/foray-planner:latest) |
| `foray_ridb_api_key` | Recreation.gov API key (optional) |
| `foray_alert_email` | DO monitoring alert recipient (from `FORAY_ALERT_EMAIL` env); unset skips alert policy creation (issue #84) |
| `foray_spaces_access_key_id` / `foray_spaces_secret_access_key` | DO Spaces access key (from `DO_SPACES_KEY` / `DO_SPACES_SECRET`, generated under API -> Spaces Keys, separate from the API token). Needed for the basemap Space; unset skips it |
| `foray_basemap_url` | Vector basemap archive URL (from `FORAY_BASEMAP_URL` env, else the computed CDN URL once the Spaces key is set, else empty = no base layer) |
| `foray_basemap_space_name` / `foray_basemap_object_key` / `foray_basemap_bbox` | Space name, archive key, and CONUS bbox for the PMTiles archive |
| `foray_terrain_url` | Terrarium DEM tile URL template for hillshade + contours (from `FORAY_TERRAIN_URL` env, else AWS Open Data's `elevation-tiles-prod`) |
| `foray_pg_tuning_enabled` / `foray_pgbouncer_enabled` | Opt-in gates (`foray:pg-tuning` tag) for cluster-config tuning and managed PgBouncer pools (issue #333) |

## Key Files

| Path | Purpose |
|---|---|
| `site.yml` | Main playbook (provision + deploy) |
| `defaults/main.yml` | All tuneable variables |
| `tasks/provision/` | DO resource creation (database, droplet, firewall, monitoring, basemap Space + CDN) |
| `tasks/provision/database_tuning.yml` | `foray:pg-tuning` - cluster-config knobs + PgBouncer pools (issue #333); ALTER ROLE / per-table / unused-index / A-B / major-version items aren't on DO's config surface and stay manual |
| `tasks/provision/build_basemap_once.yml` | `foray:build-basemap-once` - extract the US PMTiles archive and upload it to the Space (runs on localhost) |
| `tasks/deploy/` | App deployment + cron setup |
| `templates/foray.env.j2` | Runtime env file (secrets loaded from DO managed DB) |
| `meta/argument_specs.yml` | Variable documentation and types |

## Conventions

- All modules use FQCN (`ansible.builtin.*`, `community.docker.*`, `digitalocean.cloud.*`,
  `amazon.aws.*` / `community.aws.*` for the Spaces S3 data plane via `endpoint_url`)
- Variables prefixed with `foray_`
- Tags: `foray`, `foray:provision`, `foray:deploy`, `foray:cron`; opt-in tags behind `never`:
  `foray:resize` (resizes the droplet, power-cycling it), `foray:build-basemap-once` (builds +
  uploads the vector basemap archive), `foray:pg-tuning` (cluster-config knobs + PgBouncer
  pools, issue #333 - `foray_pg_tuning_enabled` / `foray_pgbouncer_enabled` gate each half), and
  the `foray:*-once` data-warm tasks
- Secrets read from environment at runtime, never committed
- Test with molecule: `uv run molecule test`

## Testing

```bash
cd infra/ansible
uv sync
uv run yamllint .
uv run ansible-lint
uv run molecule test
```
