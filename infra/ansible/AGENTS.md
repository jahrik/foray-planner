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
| `foray_pg_log_min_duration_ms` / `foray_pg_jit` / `foray_pg_max_parallel_workers_per_gather` / `foray_pg_work_mem_kb` / `foray_pg_shared_buffers_percentage` | Cluster-config tuning knobs (issue #333), applied every `foray:provision` |
| `foray_pgbouncer_api_pool_size` / `foray_pgbouncer_cron_pool_size` | Managed PgBouncer pool sizes (issue #333), created every `foray:provision` |

## Key Files

| Path | Purpose |
|---|---|
| `site.yml` | Main playbook (provision + deploy) |
| `defaults/main.yml` | All tuneable variables |
| `tasks/provision/` | DO resource creation (database + cluster-config tuning + PgBouncer pools, droplet, firewall, monitoring, basemap Space + CDN) |
| `tasks/provision/database.yml` | Cluster + app DB creation, then cluster-config tuning + PgBouncer pools (issue #333), all on every `foray:provision`. `ALTER ROLE`/per-table autovacuum are in `cache._MIGRATIONS` instead (the app's own DB role has sufficient privilege); the unused-index audit, SP-GiST vs GiST A/B, and the PG major-version upgrade aren't on DO's config surface and stay manual |
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
  uploads the vector basemap archive), and the `foray:*-once` data-warm tasks. Postgres
  cluster-config tuning + PgBouncer pools (issue #333) are NOT behind an opt-in tag - they run
  as a standard part of `foray:provision`, same as the rest of `database.yml`
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
