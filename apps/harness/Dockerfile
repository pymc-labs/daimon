FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.11 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy
WORKDIR /workspace
RUN --mount=type=bind,source=.,target=/workspace,readonly \
    uv export --locked --no-dev --package daimon-harness --extra data \
      --no-emit-workspace --no-header --no-annotate --output-file /tmp/requirements.txt >/dev/null
RUN uv venv /opt/venv && uv pip sync --python /opt/venv/bin/python \
    --require-hashes /tmp/requirements.txt
RUN --mount=type=bind,source=packages/mux,target=/src/mux,readonly \
    --mount=type=bind,source=apps/harness,target=/src/harness,readonly \
    uv build --wheel /src/mux --out-dir /tmp/wheels && \
    uv build --wheel /src/harness --out-dir /tmp/wheels && \
    uv pip install --python /opt/venv/bin/python --no-deps /tmp/wheels/*.whl

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.11 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends bash g++ \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 agent
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" HOME="/home/agent" PYTHONUNBUFFERED=1
ARG HARNESS_VERSION=development
ENV HARNESS_VERSION=$HARNESS_VERSION
WORKDIR /home/agent
USER agent
EXPOSE 8088
CMD ["python", "-m", "daimon_harness"]
