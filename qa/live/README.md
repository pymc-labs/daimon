# Daimon live QA runner

This operator tool runs external YAML scenarios against disposable Discord QA
channels. It reuses the installed `daimon-qa/qa.py` driver's token recovery,
category permission resolution, warm probe, multipart upload, snowflake-aware
turn selection and terminal classifier. It is not imported by deployed services.

Offline CI uses injected fakes; it never loads credentials or contacts Discord,
Anthropic, Cloud Logging or a deployment database. Run `uv run pytest qa/live/tests`.
The runner source is covered by the repository's strict Pyright and Ruff checks.

## Configure and validate

```bash
uv sync --all-extras --all-packages
uv run python -m qa.live example-config --config qa/live/config.local.json
uv run python -m qa.live validate --catalog /path/to/catalog
```

The catalog can contain `scenarios/*.yaml` or YAML files directly. Attachment
paths such as `fixtures/canary.pdf` resolve against the catalog root, not the
current directory. Duplicate IDs, invalid regexes, unknown kinds, missing required
arguments, missing files and invalid turn indices
fail validation before any live action. `notes` is optional descriptive metadata.
Approved catalog extensions that are not implemented are inventoried as typed PENDING entries, without
executing their supported-looking steps. This lets a supported scenario coexist
with pending extensions in one catalog. Unknown step/assertion kinds mark only their scenario PENDING, preserving the
rest of the catalog. Canary-tier unknown kinds fail validation to catch hourly
canary typos before a live run. Approved, unimplemented kinds stay PENDING. Invalid arguments for implemented kinds still fail validation. Template placeholders also remain PENDING
for unavailable values; numeric regex quantifiers stay literal.
`est_turns` is the driver's billed-turn estimate, which may differ from trigger
count when triggers coalesce. The driver must set a conservative estimate.

Set the local config's `driver_path` to the installed `daimon-qa/qa.py` driver.
Its default location is `~/.config/daimon-qa/qa.py`; the runner does not copy or
install the driver. Keep installation-specific paths in the private local config.
Edit the local config's `pricing` before approval. The example prices are sample
values, not measured turn estimates. Include the worst expected Daimon turn cost,
judge input/output token prices, and a bounded judge input allowance. Judge calls
always use the Anthropic primary from `models.backends` (`claude-haiku-5-5`), `max_tokens=300`, `temperature=0` and the
Anthropic Messages API's JSON schema output format. See the
[Anthropic structured output reference](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
The one backend map contains Claude `claude-haiku-5-5`, OpenAI `gpt-6-luna`,
and Gemini `gemini-3.8-flash`. Unknown or expensive replacements refuse.
The Gemini fallback helper advances to `gemini-flash-latest`, then
`gemini-3.5-flash-lite`, only for HTTP 503. The shipped judge transport is
Anthropic only; the other backends do not enable new paid judge transports.
A temporary staging-only override admits `claude-haiku-4-5` and its dated
`claude-haiku-4-5-20251001` snapshot until the driver confirms Haiku 5.5
deployment. Staging also accepts the primary throughout that transition.
Exact Haiku 5.5 snapshots with a valid `YYYYMMDD` suffix are admitted in model
evidence and judge responses; requests still use the primary alias. Judges
and production canaries never use the staging override. Every trigger first runs the target's read-only
`model_probe` argv command, passing the owned channel/guild/category as JSON on
stdin. Set `qa_agent_name` to the dedicated QA agent. The shipped
`python -m qa.live.model_probe` helper must execute with the chosen deployment's
settings, directly there or through an operator-reviewed IAP wrapper forwarding
stdin/stdout. It reads the same core config cascade and actual MA agent metadata;
it neither creates a turn nor changes configuration. The returned model must be
the target backend's approved Daimon model and its agent must match the QA agent. Production additionally requires a
channel-specific agent binding, refusing tenant/deployment Sonnet/Opus defaults.
An absent probe is PENDING; missing/wrong model evidence fails. The post-turn
receipt also fails the run when a model ID is missing or differs from the target backend's approved model.
Configure all required QA identities, including
the admin bot when scenarios use `as: admin`.

Staging is hard-bound to guild `1435062989119295640`, category
`1558361838960382032`, and logging project `pymc-daimon-staging`. Preflight checks
bot identity, category ownership and effective permissions before channel creation,
and warms staging's MCP service. The deployment must already allow the configured
QA bot IDs through `DAIMON_DISCORD__QA_BOT_USER_IDS`; the runner does not change it.
Both targets default to `enabled=false`; set staging enabled only after approval.
The target's read-only database environment variable is required before preflight,
so the hourly timer cannot quietly rely on rounded footers for model/accounting.

## Execute after the driver's GO

```bash
uv run python -m qa.live canary --env staging --go \
  --catalog /path/to/catalog --config qa/live/config.local.json \
  --ledger /path/to/driver/cost/ledger.jsonl --results /path/to/results
uv run python -m qa.live run --scenario QA-D1-FOLLOWUP-REUSE --go \
  --catalog /path/to/catalog --config qa/live/config.local.json \
  --ledger /path/to/driver/cost/ledger.jsonl
```

`canary` requires exactly one `tier: canary` scenario estimating two turns. A
plain `run` selects `tier: full`. Scenarios run serially and stop after the first
FAIL or PENDING. Each run creates its QA channels and deletes them after assertions,
including on exceptions, Ctrl-C and SIGTERM. `new_channel` without a ref denotes the initial owned channel. Its first `ref`
binds that channel; later refs create additional owned channels in the same QA
category. `mention` and `channel_message` can select a ref through `channel`.
Every created channel is deleted in reverse order. Production allows one channel. Failed cleanup records the channel ID in
a failing check for manual recovery. DELETE retries three times. Preflight sweeps
at most 20 `qa-*` channels carrying this runner's topic marker in the configured
QA category when their snowflake age exceeds one hour; this recovers channels
left by SIGKILL or host loss while protecting other QA workers' allocations.

`mention`, `thread_reply`, `channel_message`, `burst`, `react`, `wait` and
`wait_done` execute through the Discord backend. `thread_reply` accepts `mention: true` and `reply_to: turnN.chunkM`; a false or
omitted `mention` posts without a mention. Reply references must resolve to an
observed message in an owned conversation. A combined file and reply reference
currently returns PENDING because the reused uploader cannot encode that reference. `burst` posts mentions in the latest observed
thread, or the owned parent if there is no thread. Each trigger receives an index;
queued triggers can share a composite answer. Fingerprints exclude previous turns'
unchanged messages from follow-up evidence. The collector settles after terminal
messages stop changing and includes visible queue reactions. Trigger reactions are sampled before message/thread
reads and retained with elapsed timestamps, so an early 👀/⌛ remains available
to presence assertions after completion clears it; the final snapshot records cleanup. Time measurements
are observation bounds at the configured polling interval.
Watch timeouts preserve evidence and evaluate assertions: a silent or stuck bot
fails the canary and alerts. The fallback watch is bounded to 180 seconds, and
the service allows 1200 seconds for watches, probes, log ingestion and cleanup.

`admin` and weekly-only `restart_workers` invoke only named `admin_hooks` from
the operator config. A hook is an argv list executed without a shell; it receives
JSON on stdin: `guild_id`, `channel_id`, `role`, `args`. Hooks must scope all writes
to the supplied QA context and restore temporary staging configuration. Configure
a corresponding teardown action for persistent setup changes. No hook is shipped
that changes workers, deployment configuration or a database. Unconfigured hooks
return PENDING. Production refuses all admin/restart actions.

Discord forbids bot-to-bot DMs; `dm` is recognized and returns PENDING. Set B,
Slack, Teams and headless surfaces return PENDING before spending. Approved
`headless_interrupt` and `interrupt_within_s` are recognized and return PENDING
until the in-process staging hook exists. No unsupported capability passes.

## Assertions and evidence

The original Discord assertion kinds are implemented: response and terminal
latency, thread location/reuse, observed progress, blank messages, parent-channel
posts, silent drops, regex presence and
absence over content and all embed text, settled cards, reactions, attachment
counts/uniqueness, scoped logs, read-only SQL, and the fixed Haiku judge.
The approved filtered attachment counts (`name_pattern`) and timed reaction
observations remain PENDING until the FULL stage implements their semantics.
The approved global regex assertions are appended to every executed turn: no
`(empty response)`, raw platform/API exception copy, or `access_token=` output.

Approved placeholders resolve in text, nested admin arguments, regexes and SQL:
`{guild_id}`, `{channel_id}`, `{channel:REF}`, `{nonce}`, `{turnN.thread_id}` and
operator-supplied `{env.KEY}` values from the target's `context` config. YAML and
Markdown input fixtures are copied to a private temporary directory with their
contents substituted; source files remain unchanged and the original filename is
preserved. Missing values return PENDING. `{fork.name}` needs the later staging
readback hook and remains PENDING. Whole-run turn 0 assertions remain PENDING.
Triggers are indexed in order, including each `channel_message` and burst text.

Cloud Logging queries use the configured environment project, the turn's UTC
window and thread ID. The reader discovers request IDs (`rid`) only through
thread-scoped logs, then includes those IDs when querying core events that omit
thread IDs. Structured numeric/string IDs and GCE's JSON-string `message` wrapper
are supported. Broad wrapped-log matches are checked against exact parsed IDs
and event names before use. The reader waits 45 seconds after the observation
window before log assertions (configurable only within 30–60 seconds).
No correlated request evidence means absence is PENDING. Query failure or truncation returns PENDING, including
for absence assertions. A `db_check` executes inside a PostgreSQL read-only
transaction with a 15-second statement timeout. Configure the URL through
`DAIMON_QA_STAGING_DATABASE_URL` (or the production equivalent); it is never stored
in reports. Named binds `:guild_id`, `:channel_id`, `:thread_id`,
`:trigger_message_id` and `:tenant_id` resolve to this run's context. Single-row,
single-column results become scalars; other results are arrays of row objects.
Headless context interpolation is unsupported and returns PENDING.

Attachment uniqueness conservatively compares filename and byte size across
turns, so two independent files with identical names and sizes fail uniqueness.
The tool does not download attachments to compare hashes.

Each run writes `results/<run-id>.json` with assertions, observations, raw Discord
messages, timing, channel IDs and accounting sources, then prints exactly five
summary lines. Treat results as private QA evidence: messages and attachments can
contain test data and signed URLs. Do not commit them.

## Shared cost cap and alerts

Every run reserves its estimated turn and judge costs against the shared UTC
`ledger.jsonl` and all outstanding reservations. The cap is fixed at $10/day.
Reservations and receipt updates use `flock` on a sidecar lock, so cooperating
runner processes cannot race the cap. Other QA workers must still coordinate
through the driver's shared ledger/cap; they do not participate in this lock.

Scenario B is encoded in `schedule`: one staging Haiku canary per UTC hour, one
production Haiku canary per UTC hour in a hard-allowed internal guild, and one
catalog invocation per UTC day across environments. Claims share a locked
`<ledger>.schedule.json`; restarting the runner cannot bypass cadence. Production
still requires its separate enablement and an actual channel-specific Haiku pin.
The planned 48 daily two-turn canaries plus `catalog_budget_usd` must fit $10,
and a catalog's turn/judge estimate must fit that allocation before any call.
The example uses a sample $0.05 per-turn allowance plus $2 for the daily catalog;
the driver must replace this with verified conservative pricing. Real Sonnet/Opus
production agents are refused; they are never used to estimate a cheaper run.

Receipts read measured tokens, cache tokens and cost from read-only
`turn_outcomes`, scoped to the thread and turn window. Model IDs and token counts
remain valid evidence when `cost_usd` is NULL; accounting then retains the conservative
reservation. A missing or unapproved model still fails. Old footers can supply
rounded token/cost evidence; modern ones supply only rounded costs. If full
measurement is unavailable, the charged ledger entry conservatively retains at
least the reserved estimate and explicitly marks accounting incomplete. Missing
token counts remain null in individual usage evidence. An unexpected process kill
leaves a reservation in `<ledger>.reservations.json`; it never expires on its own.
The driver should reconcile actual usage before removing a stale reservation.
The estimate is a preflight guard, not a runtime token limiter: actual agent spend
can exceed an estimate, and is recorded rather than concealed.
If no trigger was attempted and no judge ran, the receipt records zero actual
spend and releases the reservation. An uncertain send retains the estimate.

FAIL writes a handoff inbox file and invokes the configured argv alert command
with one summary argument. Defaults point to the root Opus inbox and `tsend.sh`
with target `%618`. Persistent failure alerts once per six hours, keyed by
scenario and environment. Three consecutive PENDING runs also alert using the
same six-hour dedupe; a subsequent PASS sends recovery from either alert state.
PENDING never sends a recovery. Failed delivery does not advance dedupe state. Change
`alerts.inbox` and `alerts.command` to choose a reviewed destination.

## Hourly user timer (install only after review)

The files under `systemd/` are templates and have not been installed. Review the
catalog, prices, bot permissions and live GO first. Update the service's
`WorkingDirectory`, executable and file paths for the operator's installation.
Create the private config and optional mode-600 environment file at the paths
in the service. Then, as the operator:

```bash
mkdir -p ~/.config/systemd/user
cp qa/live/systemd/daimon-qa-canary.service ~/.config/systemd/user/
cp qa/live/systemd/daimon-qa-canary.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now daimon-qa-canary.timer
journalctl --user -u daimon-qa-canary.service
```

The template runs staging. User timers require a logged-in user or administrator
configured lingering to survive logout. A failed service records its result and
alerts; overlapping starts of the same oneshot service are avoided by systemd.
The daily cap can refuse later hourly runs if estimates or other QA exhaust it.
Production prerequisites and the separate approval are in [PROD-CANARY.md](PROD-CANARY.md).
