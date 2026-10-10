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
current directory. Duplicate IDs, invalid regexes, missing required arguments,
missing files and invalid turn indices fail validation before any live action.
`notes` is optional descriptive metadata. Approved catalog extensions that are
not implemented are inventoried as typed PENDING entries, without executing
their steps. Unknown step/assertion kinds in other tiers mark only that scenario
PENDING, preserving the rest of the catalog. Canary-tier unknown kinds fail
validation to catch hourly canary typos before a live run. Approved,
unimplemented kinds stay PENDING. Invalid arguments for implemented kinds still
fail validation. Template placeholders remain PENDING for unavailable values;
numeric regex quantifiers stay literal.
`est_turns` is the driver's billed-turn estimate, which may differ from trigger
count when triggers coalesce. The driver must set a conservative estimate.

Set the local config's `driver_path` to the installed `daimon-qa/qa.py` driver.
Its default location is `~/.config/daimon-qa/qa.py`; the runner does not copy or
install the driver. Keep installation-specific paths in the private local config.
Edit the local config's `pricing` before approval. The example prices are sample
values, not measured turn estimates. Include the worst expected Daimon turn cost,
judge input/output token prices, and a bounded judge input allowance. Judge calls
always use the Anthropic primary from `models.backends` (`claude-haiku-5-5`), `max_tokens=300` and the
Anthropic Messages API's JSON schema output format. See the
[Anthropic structured output reference](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
Haiku 5.5 rejects the deprecated `temperature` parameter, so requests omit it.
Judge execution errors become PENDING checks, preserve deterministic product
checks, and record exception type/status in run notes. Harness execution errors
also become PENDING. Every PENDING counts toward the three-consecutive-run alert
threshold, including judge and harness errors; individual unavailable runs stay
silent. A completed judge's failed verdict fails and alerts. Server/network
errors retain the conservative judge reservation when actual usage is
unavailable; requests are never retried.
The one backend map contains Claude `claude-haiku-5-5`, OpenAI `gpt-6-luna`,
and Gemini `gemini-3.8-flash`. Unknown or expensive replacements refuse.
The Gemini fallback helper advances to `gemini-flash-latest`, then
`gemini-3.5-flash-lite`, only for HTTP 503. The shipped judge transport is
Anthropic only; the other backends do not enable new paid judge transports.
A temporary staging-only override admits `claude-haiku-4-5` and its dated
`claude-haiku-4-5-20251001` snapshot until the driver confirms Haiku 5.5
deployment. Staging also accepts the primary throughout that transition.
Exact Haiku 5.5 snapshots with a valid `YYYYMMDD` suffix are admitted in model
evidence and judge responses; requests still use the primary alias. Older
configs that omit `dated_snapshots` inherit its approved value for each backend
at load time. Explicit values must match the approved policy. Judges
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
currently returns PENDING because the reused uploader cannot encode that reference. `burst` verifies the channel model once before its scheduled posts and starts a concurrent read-only watcher immediately after each trigger. Each post is scheduled from the burst start, so probes and earlier turns cannot delay later triggers. Watchers finish before channel cleanup. Message creation timestamps (or snowflakes) measure first visibility; edits measure reused cards. Timing evidence and observed `agent_subtext_headers` are retained per turn. Channel text checks read bounded bot-only history across owned channels and threads after the `since_turn` terminal answer, excluding that turn’s messages; incomplete history or empty absence evidence is PENDING. Text patterns match each content/embed component independently with `re.MULTILINE` (anchored answer patterns full-match after stripping a recorded leading QA agent subtext header), so anchored answers are not concatenated with footers. `burst` posts mentions in the latest observed
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
Unexpected harness execution, cleanup or usage exceptions are PENDING rather
than product failures. They alert only after three consecutive PENDING runs, with the same six-hour dedupe. Result JSON retains
their type, message, frames and formatted traceback without captured locals;
credential values are redacted. Read-only model probes retry one transient
transport failure; valid metadata outside the approved policy still refuses
without posting. Missing model evidence on a normally completed turn still FAILs.

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

FULL-tier `message_count` counts unique turn messages, excluding thread starters.
`fences_balanced` checks each independently rendered content/embed component.
`footer_on_last_message` requires the sole cost footer on the final message,
ordered by Discord snowflake; a file-only post cannot carry the answer's footer.
`Turn.settled` records terminal stability separately from the bounded end time,
which is also recorded on a timeout. An unsettled observation cannot prove a
message/attachment upper bound, balanced final fences, or final footer placement:
those checks stay PENDING. An already exceeded upper bound remains FAIL, and an
observed lower bound can pass. The scenario's watch timeout remains FAIL and
alerts. Missing identities or ordering stay PENDING.
`thread_name` re-fetches the owned thread and applies every supplied
`pattern`, `pattern_absent`, and `max_len` constraint; unavailable or mismatched
thread metadata stays PENDING. A missing thread also stays PENDING; pair it with
`in_thread` when thread creation itself must be a product requirement.

For `attachments`, `name_pattern` filters files before applying min/max and
uniqueness. A maximum of zero passes when no matching file was delivered, even
if other file types exist and the turn settled. Attachment uniqueness
conservatively compares filename and byte size across turns, so two independent
files with identical names and sizes fail uniqueness.
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

The handoff inbox file is the durable alert channel. Dedupe advances as soon as that file is written; tsend exit/status is retained there and in alert state. A busy composer (exit 1) is delivered-pending and does not halt a full pass or cause duplicate notices.

Unmentioned channel-history seeds are recorded in `seed_messages`, without watching, model billing, or consuming a turn number. Assertions number actual agent turns; unmentioned replies to bot messages still count as turns.

`http_check` supports unauthenticated GET status, MIME type and body-absence assertions. HTTP-only headless scenarios create no Discord channel or billed turn. GETs have a 15 s timeout and a bounded response body; unavailable contexts or truncated absence evidence stay PENDING.

Configured staging admin hooks accept structured or quoted string args via JSON stdin; the runner never executes catalog strings as shell commands. Catalog `fixtures/` args resolve at load time and YAML/Markdown copies receive context substitution in a private temporary directory. Hook JSON responses may provide string `context` bindings (for example `fork.name`). Missing hooks/bindings remain PENDING.

`cli_check` reads agent configuration through the configured `admin_hooks.cli_check`
operator command. It accepts only `daimon agents --guild <configured QA guild> get <name>`;
names must match `qa-[a-z0-9-]{1,96}`. Validated tokens in `argv` and
`read_only: true` are passed as JSON stdin, never through a shell.
The operator hook must enforce QA resource ownership and read-only behavior.
Assertions support regex `expect`, `expect_absent`, and `expect_all_of` (a list
or a substituted JSON string list). An unavailable hook, empty output, or output
over 128 KiB in UTF-8 bytes is PENDING. Malformed resolved expectations stay
PENDING without stopping other assertions. CLI checks can inspect Discord-surface
setup state without a billed turn; headless CLI workflows remain PENDING.

Timer and manual entrypoints must share `live_lock`; the default is `~/.local/state/daimon-qa/live-run.lock`. A held lock refuses a second session before any live action. File-only alerts use `alerts.command=[]` and a local `alerts.inbox`. `Ledger.charged(run_ids)` sums final receipts and excludes reservation changes from reported charges.

Staging runs require a read-only `staging.deployment_probe` argv hook. JSON stdin
contains `env: staging`, the QA `guild_id`, and `read_only: true`. Return JSON
`{"image":"<full SHA>","workers":{"daimon-discord-1":"<full SHA>",
"daimon-slack-1":"<full SHA>","daimon-teams-1":"<full SHA>",
"daimon-scheduler-1":"<full SHA>"}}` only after checking all running worker
images. Missing, mixed, or unavailable workers remain PENDING before any trigger.

Results record start/end images, scoped draining and orphan-retirement events,
and restart cards under `deployment`. A verified start followed by a missing or
mixed end probe also counts as an active rollout. Changed images, or restart evidence
without matching verified images, force `deploy-interrupted` PENDING, preserving original checks
for triage. These attempts never alert or change the pending-alert streak.
The CLI retries once with a fresh backend and judge after all worker images stay
equal for 30 seconds (bounded to five minutes), within the remaining pass and daily
budgets. Both attempts retain receipts and JSON evidence; the retry records
`retry_of`. Custom operator entrypoints should use `run_with_deploy_retry` or apply
the same bounded policy. This checks image stability, not boot or orphan-sweep
readiness. Missing observation evidence remains PENDING and never proves a product
PASS; an event-log outage with matching verified images preserves a product FAIL.
Production execution is unchanged.

An end probe that was PENDING records `end_probe_pending` in JSON and the five-line
summary. If the settle check recovers the starting SHA, restore the original
verdict and alert policy without spending on a retry. This read-only recovery
check runs even when the retry budget is exhausted. A recovered matching image
does not excuse a product failure as a deployment. Restart evidence with matching
verified images produces an alertable failure, regardless of end-probe timing.
Image evidence cannot distinguish an environment-only redeploy on the same image;
that case follows the same alertable policy. A failed event-log read remains a
separate `events_error` and restores a PENDING observation check, so image recovery
never proves a PASS with missing logs.

A confirmed pre-admission `turn.skipped.writers_none` event makes the model check
PENDING (n/a) and records zero usage plus scoped `skip_evidence`. This requires
matching guild, channel, and turn-window evidence and no observed thread or bot
messages. Silence alone never proves a skip: executed or unproven turns retain
missing-model FAIL semantics. Product silence checks still FAIL independently.

The frozen Discord batch also supports whole-run `answers_total` and
`threads_created` upper bounds, three-valued nested `any_of`, nested component
labels, current card refreshes after a wait, distinct running card-edit timestamps,
and answer chunk gaps from answer-bearing edits or message creation times.
An incomplete observation cannot prove a count or gap upper bound. Expected CLI
failures require matching declared refusal text and the hook's JSON `exit_code`;
a transport failure remains a harness error. `new_channel.guild` must resolve to the configured approved QA
guild before any setup mutation; a separate depleted-tenant scenario remains
PENDING until its target is independently approved.

An admin CLI step with `allow_fail: true` must declare `allow_fail_pattern` (a
required stdout/stderr refusal regex) and may narrow `allow_fail_exit_codes`
(default `[1]`). Both the code and pattern must match. An unexpected exit, empty
or transport-looking output, malformed exit code, or failed hook is a harness
error; an exit code alone never proves the intended refusal.
Thread counts probe every trigger even if its thread has no bot answer. Permission
errors do not prove absence. Chunk delivery uses creation timestamps except for
recorded baseline messages or messages predating the trigger. Tenant-credit CLI
mutations are refused before setup, including the shared QA guild.

Transport/crash signatures (connection refusal/reset, timeouts, DNS, TLS/SSL, 5xx,
tracebacks and unhandled exceptions) are rejected before the declared refusal regex.
Expected patterns must not match empty output. CLI args must be command strings;
tenant-credit tokens are refused even behind wrappers or global options.

`footer_cost_matches_ledger {turn, tol_usd}` and `ledger_matches_usage {turn, tol_pct}`
require complete, read-only staging billing evidence. The settled turn's exact
`usage_refs` identify per-request usage rows and ledger debit idempotency keys;
foreign, missing or duplicate rows remain PENDING. The footer check parses only
embed footer `$X used`, separately from `$X left`, and applies the declared
absolute tolerance. `<$0.001` is an interval, never a rounded scalar.

`staging.billing_probe` is a read-only argv hook receiving `{env: staging,
guild_id, read_only: true}` on stdin. It returns JSON `{guild_id, markup, source}`
from the deployed billing configuration; missing provenance is PENDING. Expected
ledger spend is independently calculated with Decimal from each request's token
counts, the QA `billing_rates` map and that markup. The default Haiku 5.5 rates
come from driver `hack/GO-pricing-check.md` (official pricing verified 2026-10-10):
input/write/read/output per million are 0.10/0.125/0.01/0.50 up to 100,000 prompt
tokens and 0.50/0.625/0.05/2.50 above that, selected per request. Unknown models or
missing independent prices remain PENDING; Daimon's own cost/price table isn't
used as the expected value. Raw request, debit, markup and rate evidence is saved
under each turn's `billing`. No billing writes are performed.
