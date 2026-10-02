# notebook-host

A standalone FastAPI process that spawns one marimo subprocess per
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
   │     │  proxy /n/<slug>/* (passthrough; marimo checks the token)
   │     ▼
   ├── marimo run  (localhost:8100) --token (own token, on stdin) --base-url /n/slug1
   ├── marimo edit (localhost:8101) --token (own token, on stdin) --base-url /n/slug2
   └── ...

Stripped-env: no Anthropic key, no DB creds, no Discord token on this VM.
```

## Trust model

**Stripped-env invariant.** This VM carries no Anthropic key, no database
credentials, no Discord token, and no Managed Agents vault material. The only
secret present at runtime is `DAIMON_NOTEBOOK__ADMIN_SECRET`, which is set via
`fly secrets set` and never committed to source.

**Every marimo subprocess has its own access token.** Notebook code runs on
this host, can connect to every other subprocess's localhost port, and can
read every slug from `ps` (it is in `--base-url`). So neither the port nor the
slug is an access boundary. Each subprocess runs with marimo's session auth on
and its own random 256-bit token. The token reaches marimo on stdin
(`--token-password-file -`), never argv. The URL the host returns is
`/n/<slug>/?access_token=<token>`; marimo checks the token, sets a session
cookie scoped to `/n/<slug>` and redirects to the bare path. A request without
the token, including one from another notebook's cell, gets marimo's login
redirect or a 401. The proxy forwards only what the browser sends (and, for
WebSockets, only its `Cookie` and `Authorization` headers). A slug keeps its
token across re-publishes in the same mode; switching between read-only and
editor mints a new one, and the editor is refused (409) on a published blog.
A blog's token is persisted in `blogs.json` (0600, host-only) so its link
survives restarts. Deleting or reaping a slug invalidates its link. marimo runs
with `-q`, so its startup banner (which prints the tokenized URL) never reaches
the slug's log, and the host's logs redact `access_token=` (plain or
url-encoded). marimo is pinned exactly in `pyproject.toml`, which the Docker
image installs from without `uv.lock`. The host refuses to boot if links would
go out over plain http to anything but localhost: set `public_url_base` to the
`https://` origin, or `allow_http_links` on a trusted private network.

**The host never follows a link the jail uid planted.** The slug root is owned
by the host (0711), so the jail uid can't rename or replace `home`,
`workspace`, `data`, `tmp`, `notebook.py` or `marimo.log`; only the
subdirectories are the uid's (0700). The host opens every directory it chowns
`O_NOFOLLOW | O_DIRECTORY` and changes it through the fd. It writes files
(`notebook.py`, attachments) `O_CREAT | O_EXCL | O_NOFOLLOW` and renames them
into place, and opens `marimo.log` `O_NOFOLLOW`. A symlink found where a
directory or file should be (possible in trees from older releases) is
unlinked, never followed. `notebook.py`, attachments and the log are 0600.

**Leftover processes and uids.** Before a slug's uid is released, and before a
slug or a blog is respawned, the host kills every process running as that uid,
not just marimo's process group: it becomes the uid and calls `kill(-1,
SIGKILL)`, then rescans `/proc` until none are left. If one survives the
deadline, the uid is quarantined (never handed out again) rather than
released. Files the uid owns in `/tmp` and `/dev/shm` are deleted too. Jailed
processes run with `PR_SET_NO_NEW_PRIVS`, a per-uid process cap
(`RLIMIT_NPROC`) and a private `TMPDIR` inside the slug's tree. Uids are
handed out round-robin, so a just-released uid is the last one reused.
Switching a slug between the editor and the read-only app wipes its `home`
(including the uv cache), `workspace` and `tmp`, so nothing the editor
planted runs under the new mode.

**Read-only by default.** A scratch notebook is served as a `marimo run` app
(code hidden, widgets live) unless its upload token asked for the editor
(`notebook_edit`, minted only for `create_notebook_upload_url(editable=True)`,
which the bot refuses unless the operator set `DAIMON_NOTEBOOK__ALLOW_EDITABLE`
on the bot). The host enforces its own `allow_editable` (same variable name,
off by default) on `notebook_edit` tokens and on `PUT /admin/notebooks/{slug}`
with `"editable": true`; without it every notebook is read-only.
An editor link runs arbitrary code as that notebook's jail uid, so treat it
as a shell on this host. Blogs are always read-only.

**Bearer auth on `/admin/*`** guards the admin API. The admin bearer is
scrubbed from every subprocess's environment.

**Per-notebook origins (`origin_base`).** A notebook's JavaScript (an
anywidget runs even in a read-only app) can reach anything on its own origin
with the viewer's cookies. So on a public host every notebook gets its own
origin: set `DAIMON_NOTEBOOK__ORIGIN_BASE=nb.example.com`, with wildcard DNS
and a wildcard TLS certificate for `*.nb.example.com`. Each link is then
`https://<label>.nb.example.com/n/<slug>/?access_token=…`, where `label` is 32
hex characters derived from the notebook's token (never the slug), so it
rotates with the token and can't be guessed. The proxy:

- routes by the exact `Host`: `/n/<slug>/` on the bare host, or on another
  notebook's origin, is a 404, so path mode is refused;
- refuses (403) any request the browser marks as coming from another origin
  (`Origin`, or a `Sec-Fetch-Site` other than `same-origin`/`none`), except a
  top-level navigation such as opening the link from chat. Page JavaScript
  can't forge these headers, and a sibling notebook is same-site but
  cross-origin, so its fetches, form posts and frames are all refused;
- accepts a WebSocket only when `Origin` is exactly the notebook's own origin;
- strips any `Domain` from `Set-Cookie`, so every cookie is host-only, and
  sends `Content-Security-Policy: frame-ancestors 'self'`;
- over https, renames marimo's cookies to `__Host-` cookies (`Secure`,
  `Path=/`, no `Domain`), forwards only `__Host-` cookies to marimo (so a
  cookie tossed from another subdomain is dropped), and sends HSTS.

`ORIGIN_BASE` must be a **dedicated registrable domain** (e.g.
`daimon-notebooks.example`, not `nb.yourcompany.com`), or be listed on the
Public Suffix List. Otherwise every other site under the same registrable
domain is same-site with the notebooks.

A real-browser test (`tests/test_notebook_origin_browser.py`) opens B's link,
then runs attacker JS on A's page: credentialed fetch, `no-cors` POST and
WebSocket to B, before and after `history.replaceState` to B's path. None of
it reaches B's marimo.

**Without `origin_base`, a host serves only the tenants you list.** All
notebooks then share one origin and there is no browser isolation between
them. A public host (anything but localhost) therefore admits uploads only
from the tenants in `DAIMON_NOTEBOOK__TENANTS`, comma-separated tenant UUIDs
or a JSON array, e.g. `<discord-tenant-uuid>,<slack-tenant-uuid>`. List only tenants
you control (say your own Discord server, Slack workspace and Teams tenant),
since they can reach each other's notebooks. Any other tenant, or a token
naming none, gets a 403 that names the setting and the refused id; with the
list empty every upload is refused. `daimon tenants list --json` shows each
tenant's `id`. The host logs a warning at boot. Local dev hosts skip the check.

**Known limits.** There is no per-notebook pid or network namespace:
subprocesses run as separate uids, but an editor notebook can see other
notebooks' process list and reach the host's network. On a public host, keep
editors off (`allow_editable`) unless the host serves one client.

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
| `allow_editable` | `DAIMON_NOTEBOOK__ALLOW_EDITABLE` | `false` *(every notebook read-only)* |
| `allow_http_links` | `DAIMON_NOTEBOOK__ALLOW_HTTP_LINKS` | `false` *(refuse to boot with plain-http links off localhost)* |
| `origin_base` | `DAIMON_NOTEBOOK__ORIGIN_BASE` | *(unset — one shared origin, only listed `tenants` on a public host; set to e.g. `nb.example.com` with wildcard DNS + TLS)* |
| `origin_scheme` | `DAIMON_NOTEBOOK__ORIGIN_SCHEME` | `https` |
| `tenants` | `DAIMON_NOTEBOOK__TENANTS` (comma-separated or JSON array) | *(empty — a public host without `origin_base` refuses every upload; ignored with `origin_base`)* |
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
