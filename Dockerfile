# syntax=docker/dockerfile:1

# Base images are pinned by tag + digest; Renovate keeps both current. The uv version is also
# pinned in .github/actions/setup-uv/action.yml (Renovate updates both in one PR).
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM python:3.14-slim@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8 AS builder

COPY --from=uv /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Dependencies first: this layer only changes with pyproject.toml/uv.lock.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# README.md and LICENSE.md are part of the package metadata (readme, license-files).
COPY README.md LICENSE.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.14-slim@sha256:35caba83af2fe6da9a5d922f9b0156f21d1246a48278c58a08bd253303ba42e8

# Commit the image was built from; recorded in manifests when git is not available.
ARG GIT_SHA=""

LABEL org.opencontainers.image.title="battery-optimizer" \
      org.opencontainers.image.description="Battery optimisation toolkit: Prometheus/Home Assistant extraction, dataset build and battery MPC benchmarks" \
      org.opencontainers.image.source="https://github.com/pando85/home-energy-optimizer" \
      org.opencontainers.image.url="https://github.com/pando85/home-energy-optimizer" \
      org.opencontainers.image.documentation="https://github.com/pando85/home-energy-optimizer#readme" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.revision="${GIT_SHA}"

# Only the virtualenv is copied: no uv, no build tools, no git, no sources, no data.
COPY --from=builder /opt/venv /opt/venv

# The base configuration (entity ids, tariff, climate configs; no secrets, no data). The default
# lookups (config/service.yaml, config/tariff.yaml relative to the working directory) find it;
# site-specific values go in a sparse BATTERY_OPTIMIZER_SERVICE_CONFIG_OVERRIDES file.
WORKDIR /app
COPY config ./config

# PYTHONDONTWRITEBYTECODE: the root filesystem may be read-only (bytecode was compiled at build time).
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BATTERY_OPTIMIZER_GIT_SHA=${GIT_SHA}

USER 65532:65532

ENTRYPOINT ["battery-optimizer"]
