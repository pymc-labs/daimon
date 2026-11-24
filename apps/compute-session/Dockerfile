# Work image for compute sessions: the thin MCP server and a Python toolchain.
# Build from the repository root (the context supplies uv.lock):
#   docker build -f apps/compute-session/Dockerfile -t compute-session .
# Nothing here is a credential. The session token arrives at run time in MCP_AUTH_TOKEN.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.11@sha256:5aa820129de0a600924f166aec9cb51613b15b68f1dcd2a02f31a500d2ede568 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy
WORKDIR /workspace
# The server's dependencies at the versions and hashes uv.lock records.
RUN --mount=type=bind,source=.,target=/workspace,readonly \
    uv export --locked --no-dev --package daimon-compute-session --no-emit-workspace \
      --no-header --no-annotate --output-file /tmp/requirements.txt >/dev/null
RUN uv venv /opt/compute-session && \
    uv pip sync --python /opt/compute-session/bin/python --require-hashes /tmp/requirements.txt
RUN --mount=type=bind,source=apps/compute-session,target=/src,readonly \
    uv build --wheel /src --out-dir /tmp/wheels && \
    uv pip install --python /opt/compute-session/bin/python --no-deps /tmp/wheels/*.whl

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
COPY --from=ghcr.io/astral-sh/uv:0.9.11@sha256:5aa820129de0a600924f166aec9cb51613b15b68f1dcd2a02f31a500d2ede568 /uv /uvx /usr/local/bin/
# bash, coreutils (timeout, base64, tail, nohup) and util-linux (setsid) run
# background commands. gcc, g++ and make compile PyMC models. tini reaps the
# detached commands: one left unreaped would be reported as running for ever.
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates coreutils curl g++ gcc git make procps tini util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10000 --shell /bin/bash work
# The server keeps its own environment, outside PATH, so that packages the
# session owner installs can neither replace nor break its dependencies.
COPY --from=builder /opt/compute-session /opt/compute-session
ENV HOME=/home/work PORT=8080 PYTHONUNBUFFERED=1
USER 10000
WORKDIR /home/work
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "-s", "--"]
CMD ["/opt/compute-session/bin/python", "-m", "compute_session"]
