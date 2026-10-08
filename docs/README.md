# Documentation

Everything about Foray Planner, by what you are trying to do. The project overview and a quick
start are in the [root README](../README.md).

## Use the app

| Page | What it covers |
|---|---|
| [How to use Foray Planner](tutorials/README.md) | Seven illustrated walkthroughs with animations: getting started, finding the best spot, tracking down a target mushroom, destination details, planning a road trip, using a phone, and reading the map (legend, layers, settings) |
| [How destinations are ranked](scoring.md) | The score, the fire and access adjustments, the season-trend label, trail relevance, the trip planner. What the numbers do and do not mean |

## Understand the system

| Page | What it covers |
|---|---|
| [Architecture](architecture.md) | Data flow, the H3 region grid, the database, ingest patterns, request path, operations. Start here before changing code |
| [Data sources](data-sources.md) | Every external dataset: what it is used for, licence and attribution, rate limits, quirks, and the bulk-snapshot pipeline |
| [Jobs, health checks and metrics](jobs.md) | `jobs.yaml`, how a scheduled job runs, dev and production schedules, `/healthz*`, `/metrics`, alerts |
| [Database performance baseline](db-performance-baseline.md) | Measured query timings on the production-scale dataset, and how to reproduce them |

## Build on it

| Page | What it covers |
|---|---|
| [Development guide](development.md) | Setup, the daily loop, tests, quality gates, the dev database, contributing, troubleshooting |
| [HTTP API reference](api.md) | Every route, its parameters and responses, caching, limits, status codes |
| [Frontend guide](frontend.md) | The TypeScript client: layout, state, the map stack, views, the typed API client, design tokens, genus icons, mobile |
| [CLI reference](cli.md) | Every `foray` command and `just` recipe |
| [Configuration](configuration.md) | Every environment variable and where it is read |

## Run it

| Page | What it covers |
|---|---|
| [Deployment](deployment.md) | Docker, the production stack (DigitalOcean, Ansible, Caddy, Cloudflare), CI/CD, observability, maintenance mode |
| [`infra/ansible/AGENTS.md`](../infra/ansible/AGENTS.md) | Ansible variables, tags and conventions |

## Guidance for contributors and tools

[`AGENTS.md`](../AGENTS.md) at the repository root is the dense, always-current reference for
people and coding assistants working in the repository: module-by-module notes, conventions and the
reasons behind them.

## Keeping the docs true

Docs describe code that changes. When you change behavior, update the page that describes it in the
same pull request; [the development guide](development.md#working-on-the-docs) lists which page
follows which change. The tutorial images are generated, not hand-edited:
`just tutorials` re-records them, see [tutorials/README.md](tutorials/README.md#updating-these-tutorials).
