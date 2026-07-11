# syntax=docker/dockerfile:1

# ---- frontend: build the Vite/TypeScript client bundle ----
FROM node:22-slim AS frontend
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

# /data is the persistent volume (DuckDB cache + runtime location.json); the curated
# species seed stays baked into the image at /app/data/species_seed.yaml.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN useradd --uid 1000 --create-home foray \
    && mkdir -p /data \
    && chown -R foray:foray /data

WORKDIR /app
COPY --from=builder --chown=foray:foray /app /app
# Overlay the built client bundle (gitignored, so not in the uv builder's context).
COPY --from=frontend --chown=foray:foray /app/src/foray/web/dist /app/src/foray/web/dist

USER foray
VOLUME ["/data"]
EXPOSE 8000

# Liveness: config endpoint returns 200 once the app is up (no curl in slim image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/config', timeout=4).status==200 else 1)"]

# config.docker.yaml points the DuckDB path at the /data volume; everything else matches
# config.yaml. Refresh runs as a separate one-off (see README / compose):
#   docker run --rm -v foray-data:/data <image> foray --config config.docker.yaml refresh
CMD ["foray", "--config", "config.docker.yaml", "serve", "--host", "0.0.0.0", "--port", "8000"]
