# report-host

A standalone FastAPI process that serves a published report — a rendered
document plus a chat sidebar backed by daimon — as its own web page. Designed
to sit beside the notebook host as a second stripped-env app: it talks to
daimon only over HTTP/MCP, and never imports the `daimon` package.

## Trust model

**Stripped-env invariant.** This process carries no Anthropic key, no
database credential, and no platform bot token. The only secrets present at
runtime are its own admin bearer and, in its own SQLite file, one seam token
per published report — both supplied at runtime, never baked into the image.

**Import boundary.** `report_host` cannot import `daimon` — enforced by an
`import-linter` forbidden contract, not just convention.

## Structure

- `src/report_host/` — the package. `__main__.py` is the uvicorn entrypoint;
  the app factory (`create_app`) and its routes land in a later plan.
- `tests/` — package tests, collected by the root pytest run and gated by a
  dedicated CI shard.
- `Dockerfile` — stripped-env container image, no daimon dependency.

## Routes

- `GET /health` — liveness probe, returns `{"ok": "true"}`.

Report and chat routes are added by later plans in this PR.

## Running locally

```bash
uv run python -m report_host
curl -sf http://localhost:8002/health
```

## Configuration

This app's settings module has not landed yet; a later plan in this PR adds
it along with the corresponding section in the root `.env.example` generator.
