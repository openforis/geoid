# syntax=docker/dockerfile:1
# One image, two entrypoints: `geoid web` (uvicorn) and `geoid migrate` (alembic).
# The same artifact serves the on-prem (docker compose) build and the FAO Cloud
# Run build — every deployment difference is expressed as GEOID_* configuration.

# ---- build stage: resolve deps with uv from the frozen lockfile -------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Install dependencies first (no project) for a cacheable layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev --extra oidc --extra ops

# Then install the project itself.
COPY . /app
# --extra ops = psycopg for scripts/dedup_vectors.py (the post-deploy identity canary job).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra oidc --extra ops

# ---- runtime stage: slim, non-root -----------------------------------------
FROM python:3.12-slim-bookworm AS runtime
# shapely wheels bundle GEOS and asyncpg needs no libpq, so no apt installs are
# required. ca-certificates for outbound TLS (Secret Manager, etc).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8000 \
    HOST=0.0.0.0
USER app
EXPOSE 8000

# Default to the web server; Cloud Run Job / compose override with: geoid migrate
CMD ["geoid", "web"]
