# Eval pod image: the production sandbox packages (defaults/environments/default.yaml),
# the gateway and every executor. Build from the repository root:
#   docker build -f apps/eval-sandbox/Dockerfile -t daimon-eval-sandbox .
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS tools
ADD --checksum=sha256:d27878741598e44dfec0155e3b07deada90b5886027970cb4402383ef0577788 \
    https://github.com/openai/codex/releases/download/rust-v0.173.0-alpha.1/codex-x86_64-unknown-linux-musl.tar.gz /tmp/codex.tar.gz
ADD --checksum=sha256:fce229dd482ec1caff15cd147a293513ffb5a52390394e52136f85b4a497c742 \
    https://github.com/google-gemini/gemini-api-cli/releases/download/v0.5.0/gemini-api_Linux_x86_64.tar.gz /tmp/gemini.tar.gz
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
COPY apps/eval-sandbox/image/agent-exec.c apps/eval-sandbox/image/exec-shell.c /tmp/
RUN tar -xOf /tmp/codex.tar.gz codex-x86_64-unknown-linux-musl > /usr/local/bin/codex && chmod 755 /usr/local/bin/codex \
    && mkdir /tmp/gemini && tar -xzf /tmp/gemini.tar.gz -C /tmp/gemini \
    && install -m 755 /tmp/gemini/gemini-api /usr/local/bin/gemini-api \
    && mkdir -p /licenses/gemini-api && cp /tmp/gemini/LICENSE* /licenses/gemini-api/ \
    && gcc -O2 -Wall -Werror -o /usr/local/bin/agent-exec /tmp/agent-exec.c \
    && for s in bash dash; do \
         gcc -O2 -Wall -Werror "-DREAL=\"/usr/libexec/eval/$s\"" -o /usr/local/bin/exec-$s /tmp/exec-shell.c; done

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.11@sha256:5aa820129de0a600924f166aec9cb51613b15b68f1dcd2a02f31a500d2ede568 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy
WORKDIR /workspace
RUN --mount=type=bind,source=.,target=/workspace,readonly \
    uv export --locked --no-dev --package daimon-eval-sandbox \
      --no-emit-workspace --no-header --no-annotate --output-file /tmp/requirements.txt >/dev/null
RUN uv venv /opt/venv && uv pip sync --python /opt/venv/bin/python --require-hashes /tmp/requirements.txt
RUN --mount=type=bind,source=packages/mux,target=/src/mux,readonly \
    --mount=type=bind,source=apps/harness,target=/src/harness,readonly \
    --mount=type=bind,source=apps/eval-sandbox,target=/src/eval-sandbox,readonly \
    for p in mux harness eval-sandbox; do uv build --wheel /src/$p --out-dir /tmp/wheels; done && \
    uv pip install --python /opt/venv/bin/python --no-deps /tmp/wheels/*.whl

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git gh iptables libcap2-bin libfaketime nodejs npm tini \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir typst duckdb pyarrow pandas numpy scipy scikit-learn matplotlib seaborn openpyxl tabulate \
    && groupadd --gid 10000 exec \
    && useradd --uid 10000 --gid exec --create-home --shell /opt/eval/agent-bin/bash exec \
    && groupadd --gid 10001 agent && useradd --uid 10001 --gid agent --create-home agent \
    && useradd --uid 61000 --user-group --no-create-home --shell /usr/sbin/nologin gateway \
    && usermod -aG agent exec \
    && install -d -o agent -g agent -m 2775 /workspace && install -d -m 755 /run/eval/state \
    && install -d -o agent -g agent -m 2775 /mnt/session/outputs
COPY --from=builder /opt/venv /opt/venv
COPY --from=tools /usr/local/bin/codex /usr/local/bin/gemini-api /usr/local/bin/
COPY --from=tools /licenses/ /usr/local/share/licenses/
COPY apps/harness/licenses/ /usr/local/share/licenses/codex/
COPY --from=tools /usr/local/bin/agent-exec /opt/eval/bin/agent-exec
COPY --from=tools /usr/local/bin/exec-bash /usr/local/bin/exec-dash /tmp/
COPY apps/eval-sandbox/image/agent-shell /opt/eval/bin/agent-shell
# Only the executor may become the agent; nothing else in the image can gain privileges.
# Every shell the executor starts, by name, path or login shell, runs as the agent. Its
# binaries are unreadable to it, so the kernel makes them undumpable: nothing they start
# can read their memory or environment.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
    && chown root:exec /opt/eval/bin/agent-exec && chmod 0750 /opt/eval/bin/agent-exec \
    && setcap cap_setuid,cap_setgid=ep /opt/eval/bin/agent-exec \
    && install -d /opt/eval/agent-bin /usr/libexec/eval \
    && ln -s /opt/eval/bin/agent-exec /opt/eval/agent-bin/bash \
    && mv /usr/bin/bash /usr/libexec/eval/bash && install -m 755 /tmp/exec-bash /usr/bin/bash \
    && mv /usr/bin/dash /usr/libexec/eval/dash && install -m 755 /tmp/exec-dash /usr/bin/dash \
    && rm /tmp/exec-bash /tmp/exec-dash \
    && chmod 0711 /usr/local/bin/codex /usr/local/bin/gemini-api \
    && chmod 755 /opt/eval/bin/agent-shell \
    && /opt/venv/bin/python -m daimon_eval_sandbox manifest > /opt/eval/manifest.json
ENV PYTHONUNBUFFERED=1
WORKDIR /workspace
EXPOSE 8088
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/venv/bin/python", "-m", "daimon_eval_sandbox", "main"]
