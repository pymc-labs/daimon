# notebook-host

A standalone FastAPI process that spawns one `marimo edit` subprocess per
published notebook and reverse-proxies HTTP + WebSocket traffic for
`/n/<slug>/*` paths. Designed for self-hosting DS teams reaching Daimon
through chat adapters (Discord/Slack) from inside a trusted network.

## Architecture

A single Fly Machine runs one FastAPI host that manages N marimo subprocesses
behind it. Each notebook gets its own port from a pool; the host reverse-proxies
all `/n/<slug>/*` traffic to the matching subprocess without URL rewriting
(marimo's `--base-url` flag makes the proxy a straight passthrough).

```
External (untrusted)
   │
   ▼
[Fly's public TLS proxy]  ← TLS terminated here; HTTP from here inward
   │
   ▼
[Single Fly Machine]
   │
   ├── FastAPI host (:8001)
   │     │  bearer-auth on /admin/*
   │     │  proxy /n/<slug>/* (no auth) — slug-as-secret
   │     ▼
   ├── marimo edit (localhost:8100) --no-token --base-url /n/slug1
   ├── marimo edit (localhost:8101) --no-token --base-url /n/slug2
   └── ...

Stripped-env: no Anthropic key, no DB creds, no Discord token on this VM.
```

## Trust model

**Stripped-env invariant.** This VM carries no Anthropic key, no database
credentials, no Discord token, and no Managed Agents vault material. The only
secret present at runtime is `DAIMON_NOTEBOOK__ADMIN_SECRET`, which is set via
`fly secrets set` and never committed to source.

**`--no-token` on marimo subprocesses** removes marimo's built-in session token
auth. This is acceptable only inside a trusted-network position where the host
is not directly reachable from the public internet. The network is the outer
perimeter.

**Bearer auth on `/admin/*`** is the only HTTP auth boundary enforced by the
host itself. The `/n/<slug>/*` paths are unauthenticated — slug-as-secret is
the deliberate per-notebook access boundary. The slug is minted as
`secrets.token_hex(6)` per publish; collision probability is negligible at
expected volume.

**Operators MUST NOT expose the notebook host to the public internet without
an upstream auth layer.** Without one, any user who obtains or guesses a slug
can access that notebook. The intended deployment topology is: Fly's TLS
termination as the public edge, with the notebook host only reachable from the
bot VM or from within a trusted network.

## Configuration

All settings use the `DAIMON_NOTEBOOK__` env prefix with `__` as the nested
delimiter.

| Setting | Env var | Default |
|---|---|---|
| `data_dir` | `DAIMON_NOTEBOOK__DATA_DIR` | `/data/notebooks` |
| `admin_secrets` | `DAIMON_NOTEBOOK__ADMIN_SECRETS` (CSV) | *(at least one bearer required; see Rotation below)* |
| `admin_secret` (legacy alias) | `DAIMON_NOTEBOOK__ADMIN_SECRET` | *(deprecated singular; auto-folded into the list)* |
| `host_port` | `DAIMON_NOTEBOOK__HOST_PORT` | `8001` |
| `marimo_port_start` | `DAIMON_NOTEBOOK__MARIMO_PORT_START` | `8100` |
| `marimo_port_end` | `DAIMON_NOTEBOOK__MARIMO_PORT_END` | `8160` |
| `subprocess_ttl_seconds` | `DAIMON_NOTEBOOK__SUBPROCESS_TTL_SECONDS` | `86400` |
| `sweep_interval_seconds` | `DAIMON_NOTEBOOK__SWEEP_INTERVAL_SECONDS` | `300` |
| `spawn_timeout_seconds` | `DAIMON_NOTEBOOK__SPAWN_TIMEOUT_SECONDS` | `20` |
| `validate_on_publish` | `DAIMON_NOTEBOOK__VALIDATE_ON_PUBLISH` | `true` (run `marimo export` before serving, catching notebooks that fail to execute) |
| `validation_timeout_seconds` | `DAIMON_NOTEBOOK__VALIDATION_TIMEOUT_SECONDS` | `60` (wall-clock budget for that validation export; a slow-but-valid notebook is published anyway) |
| `public_host` | `DAIMON_NOTEBOOK__PUBLIC_HOST` | `localhost` |
| `public_url_base` | `DAIMON_NOTEBOOK__PUBLIC_URL_BASE` | *(unset — set when behind a TLS terminator that strips the internal port, e.g. Fly's https edge)* |
| `max_source_bytes` | `DAIMON_NOTEBOOK__MAX_SOURCE_BYTES` | `1048576` (1 MiB) |
| `max_attachment_bytes_ceiling` | `DAIMON_NOTEBOOK__MAX_ATTACHMENT_BYTES_CEILING` | `104857600` (100 MiB; host-side hard ceiling, defense-in-depth above the daimon-side cap) |
| `allowed_origins` | `DAIMON_NOTEBOOK__ALLOWED_ORIGINS` | *(empty — check disabled)* |
| `marimo_rlimit_as_bytes` | `DAIMON_NOTEBOOK__MARIMO_RLIMIT_AS_BYTES` | `4294967296` (4 GiB) |
| `marimo_rlimit_cpu_seconds` | `DAIMON_NOTEBOOK__MARIMO_RLIMIT_CPU_SECONDS` | `3600` |
| `pids_file` | `DAIMON_NOTEBOOK__PIDS_FILE` | *(defaults to `<data_dir>/pids.json`)* |
| `blogs_file` | `DAIMON_NOTEBOOK__BLOGS_FILE` | *(defaults to `<data_dir>/blogs.json`)* |

### Rotating the admin bearer

`admin_secrets` accepts multiple bearers as CSV. To rotate without 401-ing the bot:

1. Append the new bearer alongside the old: `DAIMON_NOTEBOOK__ADMIN_SECRETS="old-token,new-token"`. Redeploy the host.
2. Switch the bot to the new bearer (`DAIMON_NOTEBOOK__ADMIN_SECRET=new-token` on the bot side). Redeploy.
3. Drop the old bearer from the host: `DAIMON_NOTEBOOK__ADMIN_SECRETS="new-token"`. Redeploy.

Both bearers are checked with `hmac.compare_digest` and the loop runs to completion regardless of where the match lands — list position does not leak via timing.

`allowed_origins` is a comma-separated list (e.g. `"https://nbs.example.com,https://nbs-staging.example.com"`). When set, the WebSocket reverse-proxy route `/n/<slug>/ws` rejects upgrades whose `Origin` header is not in the list (including upgrades with no `Origin`). When empty (default), the check is disabled — appropriate for trusted-network deployments where the host is not browser-reachable from outside. Set this if the host is ever exposed to a public network where a leaked slug could be opened by a malicious page in a user's browser.

## Installing Python libraries for published notebooks

The marimo subprocesses spawned by the host run in the same Python
environment that started the host process. To make a library available to
published notebooks, install it into that environment — not into a separate
notebook venv.

Two optional extras are shipped with this app for the common cases:

```bash
# Generic DS stack: pandas, numpy, scikit-learn, matplotlib, scipy
uv pip install -e 'apps/notebook-host[ds]'

# PyMC + ArviZ stack (pinned to the pre-1.0 ArviZ line — see below)
uv pip install -e 'apps/notebook-host[ds,pymc]'
```

**ArviZ pin rationale.** PyMC 6 and ArviZ 1.x are the in-flight major
releases — top-level helpers like `az.plot_posterior` are being
reorganised across the `arviz`, `arviz-base`, and `arviz-plots` packages,
and most of the pymc-examples corpus still targets the 0.x surface. The
`[pymc]` extra pins `pymc<6` and `arviz<1` so published notebooks line up
with what current tutorials and the agent's notebook skill assume. Lift
the pin once we're done migrating example notebooks and the agent skill
to the new entry points.

If you install additional libraries ad-hoc with `uv pip install`, restart
already-running marimo subprocesses (re-publish their slug, or restart the
host) so they pick up the new modules.

## Running locally

```bash
DAIMON_NOTEBOOK__ADMIN_SECRET=dev-secret \
DAIMON_NOTEBOOK__DATA_DIR=/tmp/notebooks \
  uv run python -m notebook_host
```

Publish a notebook via the admin API:

```bash
curl -s -X PUT http://localhost:8001/admin/notebooks/my-slug \
  -H "Authorization: Bearer dev-secret" \
  -H "Content-Type: application/json" \
  -d '{"source": "import marimo\napp = marimo.App()\n"}' | jq .
# → {"slug":"my-slug","url":"http://localhost/n/my-slug/","port":8100,...}
```

Open the returned `url` in a browser to reach the marimo session.

## Deployment

Build and deploy using the files in this directory:

- `apps/notebook-host/Dockerfile` — stripped-env container image.
- `apps/notebook-host/fly.notebook.example.toml` — Fly config template with
  placeholders. **Copy and edit before deploying** — the committed file
  contains `<your-app-name>` placeholders that will produce broken URLs if
  used verbatim.

```bash
cp apps/notebook-host/fly.notebook.example.toml apps/notebook-host/fly.notebook.toml
# Edit fly.notebook.toml: replace every <your-app-name> with the Fly app
# name you intend to create (e.g. <your-notebook-host-app>).
# Recommended: add fly.notebook.toml to .gitignore so per-deployment values
# never land in the upstream repo.
```

Set the bot's `DAIMON_NOTEBOOK__HOST_URL` to the internal Fly URL (e.g.
`http://<your-app-name>.internal:8001`) so that the `publish_notebook` MCP
tool can reach the host's admin API.

Deploy:

```bash
fly apps create <your-app-name>
fly volumes create notebook_data --app <your-app-name> --region ord --size 10
fly secrets set DAIMON_NOTEBOOK__ADMIN_SECRET=<secret> --app <your-app-name>
fly deploy --config apps/notebook-host/fly.notebook.toml --app <your-app-name>
```

## What is intentionally NOT here

**AST allowlist filter.** A filter that statically inspects notebook source
and blocks disallowed imports/calls would matter for untrusted public-trial
users who are strangers to the operator. DS teams running `pandas`, `sklearn`,
and `sqlalchemy` notebooks would hit such a filter constantly. Dropped
entirely for the self-hosted audience, where the notebook author and the
operator are the same trust domain.

**Trial-quota / rate-limit model.** A per-user quota and rate-limit system is
a SaaS construct that self-hosters don't need to meter against themselves.
Closes issue #28 as not-a-gap.

**Per-notebook filesystem skill bundler.** A component that copies skill
files into a per-notebook filesystem sandbox isn't needed here: this fork
uses MA-resolved skills via `defaults/skills/` as first-class server-side
resources, so no on-disk skill materialization is required.

**Public-trial flow.** The threat model for public trials (anonymous users,
shared infra, aggressive quotas) is different from trusted-team self-hosting.
Revisit in a fresh phase if ever needed.
