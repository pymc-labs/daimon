# ---------------------------------------------------------------------------
# Stage 1: builder — install build deps + compile wheels
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

COPY pyproject.toml uv.lock* ./
COPY packages/ ./packages/

RUN uv sync --frozen --no-dev --extra billing

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

# Copy venv from builder (includes all installed packages via UV_LINK_MODE=copy)
COPY --from=builder --chown=daimon:daimon /app/.venv ./.venv
COPY --from=builder --chown=daimon:daimon /app/pyproject.toml ./pyproject.toml
COPY --from=builder --chown=daimon:daimon /app/packages/ ./packages/

# App config + data
COPY --chown=daimon:daimon alembic.ini ./
COPY --chown=daimon:daimon defaults/ ./defaults/

# Entrypoint script (runs `daimon defaults apply` then exec's command)
COPY --chown=daimon:daimon docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER daimon

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# No CMD — docker-compose services or fly.toml [processes] supply the command.
