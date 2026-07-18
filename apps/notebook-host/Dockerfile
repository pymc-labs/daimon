# Stripped-env notebook host. NO Anthropic key, NO DB creds, NO Discord token
# bake into this image. The only secret is DAIMON_NOTEBOOK__ADMIN_SECRET,
# supplied at runtime via `fly secrets set` or an equivalent mechanism.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.11 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY apps/notebook-host/pyproject.toml ./pyproject.toml
COPY apps/notebook-host/src ./src

# [ds] adds pandas/numpy/scikit-learn/matplotlib/scipy so the marimo_notebooks
# skill's "available libraries" promise holds. [pymc] adds pymc + arviz pinned
# to the stable 5.x/0.x line (see pyproject.toml comment). Both extras live in
# the same venv the marimo subprocesses inherit at runtime.
RUN uv sync --no-dev --extra ds --extra pymc

RUN mkdir -p /data/notebooks
ENV DAIMON_NOTEBOOK__DATA_DIR=/data/notebooks

EXPOSE 8001

CMD ["uv", "run", "python", "-m", "notebook_host"]
