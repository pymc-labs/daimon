# Stripped-env report host. Runtime secrets are supplied only at startup.
FROM mirror.gcr.io/library/python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy
WORKDIR /workspace

# The bind mount exposes workspace manifests without committing other packages
# or applications to an image layer.
RUN --mount=type=bind,source=.,target=/workspace,readonly \
    uv export --locked --no-dev --package report-host --no-emit-workspace \
      --no-header --no-annotate --output-file /tmp/report-host-requirements.txt \
      >/dev/null

RUN uv venv /opt/report-host && \
    uv pip sync --python /opt/report-host/bin/python --require-hashes \
      /tmp/report-host-requirements.txt

# Build only this project and install its wheel without resolving dependencies.
RUN --mount=type=bind,source=apps/report-host,target=/src,readonly \
    uv build --wheel /src --out-dir /tmp/wheels && \
    uv pip install --python /opt/report-host/bin/python --no-deps /tmp/wheels/*.whl

# Verify the installed closure against the lock export. The checker's own
# dependency is isolated and never copied into the runtime image.
COPY scripts/check_report_host_lock.py /tmp/check_report_host_lock.py
COPY apps/report-host/pyproject.toml /tmp/report-host-pyproject.toml
RUN uv venv /tmp/checker && \
    uv pip install --python /tmp/checker/bin/python packaging && \
    /tmp/checker/bin/python /tmp/check_report_host_lock.py \
      /tmp/report-host-requirements.txt /opt/report-host/bin/python \
      /tmp/report-host-pyproject.toml

FROM mirror.gcr.io/library/python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /data/reports

COPY --from=builder /opt/report-host /opt/report-host
ENV DAIMON_REPORT__DATA_DIR=/data/reports \
    PATH="/opt/report-host/bin:$PATH"

EXPOSE 8002
CMD ["/opt/report-host/bin/python", "-m", "report_host"]
