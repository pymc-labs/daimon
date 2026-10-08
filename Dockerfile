# ---------------------------------------------------------------------------
# Stage 0: manifest — the root pyproject without its tool tables
# ---------------------------------------------------------------------------
# Ruff, pyright, import-linter and pytest settings change far more often than
# dependencies. COPY keys on content, so dropping them here keeps the builder
# below cached across those edits.
FROM python:3.12-slim AS manifest
COPY pyproject.toml /src/pyproject.toml
RUN awk '/^\[/ { keep = /^\[(project|dependency-groups|tool\.uv)[].]/ } keep' \
    /src/pyproject.toml > /pyproject.toml

# ---------------------------------------------------------------------------
# Stage 1: builder — install build deps + compile third-party wheels
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.11 /uv /usr/local/bin/uv

WORKDIR /app

COPY --from=manifest /pyproject.toml ./
# World-readable: the runtime stage mounts uv.lock for its non-root sync.
COPY --chmod=644 uv.lock* ./

# Third-party dependencies only: nothing here reads the source tree, so the
# venv this stage produces changes only with uv.lock or the project's
# dependency tables.
RUN uv sync --frozen --no-dev --extra billing --no-install-workspace

# ---------------------------------------------------------------------------
# Stage 2: runtime — slim image, no build tools
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Non-root runtime user
RUN useradd --uid 1000 --create-home daimon

# uv binary needed for `uv run alembic` in init service
COPY --from=ghcr.io/astral-sh/uv:0.9.11 /uv /usr/local/bin/uv

WORKDIR /app

# The dependency venv is its own layer (~360MB) and stays byte-identical
# across source-only changes, so a host pulling a new image fetches only the
# small layers below it.
COPY --from=builder --chown=daimon:daimon /app/.venv ./.venv
COPY --chown=daimon:daimon pyproject.toml ./pyproject.toml

# App config + data
COPY --chown=daimon:daimon alembic.ini ./

# Entrypoint script (runs `daimon defaults apply` then exec's command)
COPY --chown=daimon:daimon docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

COPY --chown=daimon:daimon defaults/ ./defaults/
COPY --chown=daimon:daimon packages/ ./packages/

USER daimon

# Install the workspace packages (editable, pure Python) into the venv above.
# uv.lock is mounted, not copied, so the image carries the same files as
# before the dependency split.
RUN --mount=type=bind,from=builder,source=/app/uv.lock,target=/app/uv.lock \
    UV_LINK_MODE=copy uv sync --frozen --no-dev --extra billing --no-cache

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# No CMD — docker-compose services or fly.toml [processes] supply the command.
