# report-host

A standalone FastAPI process that serves one published report: a PDF in a
viewer, with a chat sidebar backed by daimon, behind a per-recipient link and
a per-report spending budget. It talks to daimon only over HTTP, through the
MCP seam exposed by the daimon MCP adapter — it holds no Anthropic key and no
database credential. The only secrets present at runtime are its own admin
bearer and, in its own SQLite file, one seam token per published report.

Designed to sit beside the notebook host as a second stripped-env app: same
shape, same trust model, no import of the `daimon` package (enforced by an
`import-linter` forbidden contract, not just convention).

## Running it locally

```bash
DAIMON_REPORT__ADMIN_SECRETS=dev-secret \
DAIMON_REPORT__MCP_URL=http://localhost:8000/mcp \
DAIMON_REPORT__PUBLIC_URL_BASE=http://localhost:8002/ \
DAIMON_REPORT__DATA_DIR=/tmp/reports \
  uv run python -m report_host
```

Then publish a report through the admin API (see Routes below) and open the
`GET /r/{slug}` link it returns for the first recipient. `GET /health`
answers `{"ok": true}` with no credential — it is the compose and
reverse-proxy liveness probe.

## Environment

The repository-wide `.env.example` generator walks the core settings model
only; it cannot see a standalone app's own settings. This table is the
canonical, human-maintained record of every variable `report_host.config`
reads — **update it whenever `config.py` changes**, since nothing else checks
that they stay in sync except this README's own test.

| Setting | Env var | Default | What it controls |
|---|---|---|---|
| `data_dir` | `DAIMON_REPORT__DATA_DIR` | `/data/reports` | Root of the persistent volume: the SQLite file, per-report bundle archives, and PDF revisions. |
| `admin_secrets` | `DAIMON_REPORT__ADMIN_SECRETS` (CSV) | *(required — the host refuses to start without at least one)* | Bearer tokens accepted on admin routes. Provide more than one to rotate without downtime. |
| `mcp_url` | `DAIMON_REPORT__MCP_URL` | *(required)* | The seam endpoint this host calls to run turns, poll cost, and push bundles. |
| `public_url_base` | `DAIMON_REPORT__PUBLIC_URL_BASE` | *(required)* | External URL prefix used to build recipient links and the per-turn upload URL handed to the agent. |
| `host_port` | `DAIMON_REPORT__HOST_PORT` | `8002` | Port the host's uvicorn server binds. |
| `reserve_usd` | `DAIMON_REPORT__RESERVE_USD` | `0.60` | Amount held against a report's spend cap the moment a turn starts, replaced by the real cost once it settles. |
| `poll_interval_seconds` | `DAIMON_REPORT__POLL_INTERVAL_SECONDS` | `2.0` | How often the host polls the seam for turn progress. |
| `turn_timeout_seconds` | `DAIMON_REPORT__TURN_TIMEOUT_SECONDS` | `1200` | A turn still running past this many seconds is cancelled by the host. |
| `max_pdf_bytes` | `DAIMON_REPORT__MAX_PDF_BYTES` | `52428800` (50 MiB) | Hard ceiling on an uploaded revised-PDF body size. |
| `max_bundle_bytes` | `DAIMON_REPORT__MAX_BUNDLE_BYTES` | `26214400` (25 MiB) | Hard ceiling on a published report bundle; mirrors and independently enforces the seam's own bundle cap. |
| `max_open_threads_per_recipient` | `DAIMON_REPORT__MAX_OPEN_THREADS_PER_RECIPIENT` | `3` | Cap on concurrently open threads a single recipient may hold. |
| `max_running_turns_per_report` | `DAIMON_REPORT__MAX_RUNNING_TURNS_PER_REPORT` | `4` | Cap on concurrently running turns across one report's threads. |
| `recipient_link_ttl_days` | `DAIMON_REPORT__RECIPIENT_LINK_TTL_DAYS` | `90` | How long a per-recipient link remains valid before expiring. |
| `thread_idle_archive_hours` | `DAIMON_REPORT__THREAD_IDLE_ARCHIVE_HOURS` | `24` | A thread idle longer than this is archived, upstream then locally, by the host's background sweep. |

## Routes

Every route below requires exactly one credential format; none accepts more
than one.

- `GET /health` — liveness probe. No credential.
- `GET /r/{slug}?k=<token>` — the viewer + chat sidebar. Per-recipient link
  token, also accepted as a cookie set on first visit.
- `GET /api/{slug}/state`, `GET /api/{slug}/threads/{id}` — read state and
  thread history. Per-recipient link token.
- `POST /api/{slug}/ask`, `POST /api/{slug}/threads/{id}/cancel`,
  `POST /api/{slug}/threads/{id}/close` — drive a conversation. Per-recipient
  link token.
- `GET /files/{slug}/{name}` — fetch a PDF revision. Per-recipient link token.
- `PUT /upload/{turn_token}` — the one-time revised-PDF upload URL handed to
  the agent for a single turn. Per-turn, single-use token.
- `PUT /publish/{capability_token}` — publish a report bundle (a gzip
  archive). Single-use capability token minted by daimon.
- `PUT /admin/reports/{slug}`, `DELETE /admin/reports/{slug}`,
  `DELETE /admin/reports/{slug}/recipients/{token}` — create, update, delete
  a report and revoke a recipient link. Admin bearer.

## Operational notes

- **Bundle archives are reclaimed by daimon's scheduler, not by this host.**
  A bundle pushed at publish time is deleted upstream on a retention timer
  run by daimon's own scheduler process. An operator who does not run that
  scheduler will accumulate uploaded archives indefinitely — this host has no
  cleanup path of its own for them.
- **Every reader thread is a live upstream session until it is archived.**
  The idle sweep (`thread_idle_archive_hours`) does this automatically, but a
  host that is stopped for longer than that window before the sweep runs
  will re-attach to, rather than lose, any turn still in flight on restart.
- **A report whose seam token stops being accepted is marked unauthorized in
  its own state**, and the sidebar tells the reader the report needs
  re-publishing. The fix is a re-publish through the admin API, which mints a
  fresh token.

## Data

The persistent volume (`data_dir`) holds:

- `host.sqlite` — reports, recipients, revisions, threads and messages.
- one directory per report slug, holding its published bundle archive and
  every PDF revision served to readers.

The SQLite file holds one **seam token per report**, at the same trust level
as the admin bearer itself — either one lets a caller act as that report
against daimon. **This volume must not be shared with another service.**

## Trust model

**Stripped-env invariant.** This process carries no Anthropic key, no
database credential, and no platform bot token — the only secrets present at
runtime are the admin bearer and the per-report seam tokens described above.

**Import boundary.** `report_host` cannot import `daimon` — enforced by an
`import-linter` forbidden contract, not just convention.

## Structure

- `src/report_host/` — the package: `config.py` (settings), `mcp_client.py`
  (the seam calls), `reports_store.py` / `threads_store.py` (SQLite),
  `capability.py` (publish-token verification), `turns.py` (the per-thread
  turn driver), `routes.py` / `admin.py` / `uploads.py` (the reader, admin
  and upload routers), `sweeps.py` (restart-resume, deadline-cancel,
  idle-archive, recipient-prune), `main.py` (the app factory and lifespan),
  `viewer/` (the static PDF viewer and chat sidebar).
- `tests/` — package tests, collected by the root pytest run and gated by a
  dedicated CI shard.
- `Dockerfile` — stripped-env container image, no daimon dependency.
