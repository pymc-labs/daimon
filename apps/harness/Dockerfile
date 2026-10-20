FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.11@sha256:5aa820129de0a600924f166aec9cb51613b15b68f1dcd2a02f31a500d2ede568 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy
WORKDIR /workspace
RUN --mount=type=bind,source=.,target=/workspace,readonly \
    uv export --locked --no-dev --package daimon-harness --extra data \
      --no-emit-workspace --no-header --no-annotate --output-file /tmp/requirements.txt >/dev/null
RUN uv venv /opt/venv && uv pip sync --python /opt/venv/bin/python \
    --require-hashes /tmp/requirements.txt && \
    /opt/venv/bin/python -c 'from azure.identity.aio import DefaultAzureCredential; DefaultAzureCredential()'
RUN --mount=type=bind,source=packages/mux,target=/src/mux,readonly \
    --mount=type=bind,source=apps/harness,target=/src/harness,readonly \
    uv build --wheel /src/mux --out-dir /tmp/wheels && \
    uv build --wheel /src/harness --out-dir /tmp/wheels && \
    uv pip install --python /opt/venv/bin/python --no-deps /tmp/wheels/*.whl

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY --from=ghcr.io/astral-sh/uv:0.9.11@sha256:5aa820129de0a600924f166aec9cb51613b15b68f1dcd2a02f31a500d2ede568 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends bash g++ tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10000 harness \
    && useradd --no-create-home --uid 10001 agent \
    && mkdir -p /home/harness && chmod 711 /home/harness
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" HOME="/home/harness" PYTHONUNBUFFERED=1
ARG HARNESS_VERSION=development
ENV HARNESS_VERSION=$HARNESS_VERSION
WORKDIR /home/harness
EXPOSE 8088
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "daimon_harness"]
