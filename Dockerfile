# Container image for the weather-tracking service (tracker + web).
#
# Base: python:3.12-slim — multi-arch (amd64/arm64), so this builds on this
# host (aarch64) as well as CI/production amd64 hosts without changes.
#
# The package is installed with its `weather` extra
# (`pip install ".[weather]"`). NOTE: as of this task, `weather` is not yet
# declared in pyproject.toml — a later integration task adds it. This
# Dockerfile is written ahead of that so the image definition is ready; per
# the brief for this task, building/running this image is explicitly out of
# scope here (no `docker build`, no `docker compose up`).
#
# Both the tracker and web entrypoints live in this one image; which
# process runs is selected by `command:` in docker-compose.yml, not by
# separate Dockerfiles.
FROM python:3.12-slim

# Non-root user — least privilege for a long-running network service.
RUN groupadd --gid 1000 climate && \
    useradd --uid 1000 --gid climate --shell /usr/sbin/nologin --create-home climate

WORKDIR /app

# Copy only what's needed to resolve and install the package first, so
# dependency layers cache independently of application source changes.
COPY pyproject.toml README.md ./
COPY climate ./climate

RUN pip install --no-cache-dir ".[weather]"

# Read-only user config is bind-mounted at runtime (see docker-compose.yml);
# no default config is baked into the image.
RUN mkdir -p /app/config && chown -R climate:climate /app

USER climate

# No default CMD/ENTRYPOINT: docker-compose.yml sets `command:` per service
# (`python -m climate.weather.tracker` or `python -m climate.weather.web`).
