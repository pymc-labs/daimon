# Stripped-env report host. NO Anthropic key, NO database credential, NO
# platform bot token bake into this image. The secrets it holds at runtime
# are the admin bearer and, in its own SQLite file, one seam token per
# published report — both supplied at runtime, never baked in.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.11 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY apps/report-host/pyproject.toml ./pyproject.toml
COPY apps/report-host/src ./src

RUN uv sync --no-dev

RUN mkdir -p /data/reports
ENV DAIMON_REPORT__DATA_DIR=/data/reports

EXPOSE 8002

# Runs the report_host package's __main__ entrypoint.
CMD ["uv", "run", "python", "-m", "report_host"]
