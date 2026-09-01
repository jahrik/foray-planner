# syntax=docker/dockerfile:1

# ---- frontend: build the Vite/TypeScript client bundle ----
FROM node:26-slim AS frontend
WORKDIR /app/frontend

# Install deps first, keyed only on the lockfiles, so source edits don't bust the cache.
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

# Then the client sources. `npm run build` type-checks and emits the bundle to
# ../src/foray/web/dist (i.e. /app/src/foray/web/dist), copied into the runtime below.
COPY frontend/ ./
RUN npm run build

# ---- builder: resolve + install deps and the project with uv ----
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

# Byte-compile for faster cold starts; copy (not symlink) so /app/.venv is self-contained;
# use the image's system Python rather than a uv-managed download (keeps the venv's python
# symlink valid when copied into the runtime stage).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install dependencies first, in their own cached layer keyed only on the lockfiles, so
# source edits don't bust the dependency cache.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# Then the project itself (editable by default, keeping src/ importable at /app/src).
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime: slim image, non-root, app + venv only ----
FROM python:3.13-slim-bookworm AS runtime

# No local volume needed: the DB is Postgres, reached via the standard PGHOST/PGPORT/PGUSER/
# PGPASSWORD/PGDATABASE env vars (never baked into the image). No fixed target-genus list
# either (issue #79 Phase 4) - the full Fungi catalog lives in Postgres (fungi_genera).
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FORAY_HOME__RADIUS_KM=400 \
    FORAY_CELL_DEG=0.5

RUN useradd --uid 1000 --create-home foray

# rasterio (scripts/backfill_elevation_dem.py) ships GDAL in its wheel, but that bundled GDAL
# still dynamically loads the system libexpat, which python:slim dropped to save space - so
# `import rasterio` fails with `libexpat.so.1: cannot open shared object file`. Installing
# libexpat1 is the rasterio maintainer's own recommended fix (rasterio discussions #3257).
# Unpinned deliberately: it's a security-tracked lib, apt should pull the patched version, and
# a pin would 404 the moment Debian supersedes it (see .hadolint.yaml for DL3008). The
# `import rasterio` check below fails the build loudly if the package ever goes missing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder --chown=foray:foray /app /app
# Overlay the built client bundle (gitignored, so not in the uv builder's context).
COPY --from=frontend --chown=foray:foray /app/src/foray/web/dist /app/src/foray/web/dist

# Fail the build (not a prod one-off) if a native dep of an optional-at-runtime import is
# missing. -B so this root-run check leaves no root-owned __pycache__ in the uid-1000 venv.
RUN python -B -c "import rasterio, rasterio.sample"

USER 1000
EXPOSE 8000

# Liveness: config endpoint returns 200 once the app is up (see scripts/healthcheck.py).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "scripts/healthcheck.py"]

# Refresh runs as a separate one-off against the same Postgres, concurrently with the live
# server (no DuckDB-style single-writer lock to work around):
#   docker run --rm -e PGHOST=... <image> foray refresh
CMD ["foray", "serve", "--host", "0.0.0.0", "--port", "8000"]
