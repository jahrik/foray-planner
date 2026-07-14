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

## Key Files

| Path | Purpose |
|---|---|
| `site.yml` | Main playbook (provision + deploy) |
| `defaults/main.yml` | All tuneable variables |
| `tasks/provision/` | DO resource creation (database, droplet, firewall) |
| `tasks/deploy/` | App deployment + cron setup |
| `templates/foray.env.j2` | Runtime env file (secrets loaded from DO managed DB) |
| `meta/argument_specs.yml` | Variable documentation and types |

## Conventions

- All modules use FQCN (`ansible.builtin.*`, `community.docker.*`, `community.digitalocean.*`)
- Variables prefixed with `foray_`
- Tags: `foray`, `foray:provision`, `foray:deploy`, `foray:cron`
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
