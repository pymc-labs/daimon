## Offline fleet outcome scoring

The existing `outcome_oracle.evaluate` accepts a `fleet_outcome` assertion and
optional `RunEvidence.fleet`. Ordinary recordings omit that field. This is the
behavioral I7 scorer; it supplies no live authority, financial certification,
source-drift decision or cleanup proof.

Bind the assertion to the signed `attempt_id`, `run_id`, original
`root_session_id`, `root_binding_id` and actual `root_card_id`. Populate
`FleetEvidence` from the real `FleetRun` and `FleetWorker` rows, provider bindings,
acknowledged native inputs, authoritative native terminals and full journal
`Event` objects. Each `FleetWorkerCapture`, `FleetWakeCapture` and
`FleetCollectCapture` carries the same attempt/run/root scope. Wakes retain the
owner's `FrozenWorkerBatch`; collect retains the actual public response. All
observation times share the runner's monotonic clock. Set capture-complete flags
only when the corresponding owner confirms complete coverage. Root turns and
Discord post history use the existing `TurnEvidence` and acknowledged delivery
capture; no placeholder card or native outcome counts.

```python
report = evaluate(
    captured_run,
    [{"kind": "fleet_outcome", "attempt_id": signed_attempt,
      "run_id": actual_run_id, "root_session_id": original_root_session,
      "root_binding_id": original_root_binding, "root_card_id": actual_card_id}],
    finding_judge=offline_judge,
)
```

`finding_judge(role, full_text, expected_finding)` returns exactly `PASS`, `FAIL`
or `PENDING`. Bind it explicitly to the runner's semantic judge. It receives each
worker's full authoritative payload, never its truncated excerpt or the root's
substitute answer. W1 must reject unknown usage being treated as zero and retain
the bound; W2 must reject permission expansion; W3 must recommend retaining
bounds, attenuating grants and joining genuine native/platform evidence. Root
delivery must integrate these findings and the exact approved cross-review.
Equivalent wording is allowed. No keyword matcher or default approval is
installed; an absent or unavailable judge stays PENDING.

The scorer reuses `core.fleet.check_result` to join native completion, durable
success and the worker's own result pointer/session/binding. W4 must approve the
exact W1 digest, with duplicate JSON keys and extra verdict fields refused.
Its native start must follow the initial cohort's completion, first frozen wake
and resumed original-root input. Queued dispatch may begin after that root turn
finishes; missing timing remains PENDING, and a known early start fails.
The text-contract slice also checks three overlapping native worker intervals,
two correctly scoped frozen wakes, the original root's three inputs, exact
collected results, finalized card identity and complete actual Discord delivery.
Other result formats need their owner binding before use with this slice.

`OutcomeReport.status` remains FAIL before PENDING before PASS: an established
wrong finding, failed native/durable worker, refused review, foreign identity or
failed delivery survives missing downstream evidence. Missing coverage or
binding alone stays PENDING. The scorer cannot authenticate caller-authored
captures; the executor must supply owner evidence, not fixture declarations.

# Discord surface capture

`daimon.testing.discord_surface.DiscordSurfaceHarness` captures offline Discord
posts through the unchanged `DiscordTurnLifecycle` and `DiscordPostTransport`.
Install the Discord adapter alongside `daimon-testing` (the workspace already
includes it). Import this module explicitly; ordinary testing imports do not
require Discord.

The harness creates real `discord.py` client, thread and message objects, with an
in-process HTTP gateway replacing their network boundary. It never logs in,
reads credentials or opens a Discord connection. Unsupported gateway operations
raise rather than falling through to a real client. Provider traffic in a test
must separately use its offline transport.

```python
from daimon.testing.discord_surface import DiscordSurfaceHarness

capture = DiscordSurfaceHarness(
    evidence_id="scenario:turn:discord",
    session_id=acknowledged_session_id,
    root_turn_id=acknowledged_root_id,
    clock=runner_clock,
    render_tables=True,
)

async def invoke(lifecycle):
    await lifecycle.post_initial()
    return await offline_host_turn(lifecycle=lifecycle)

receipt = await capture.run(invoke)
```

Bind the lifecycle to the actual turn invocation. The invocation's returned value
is ignored. The runner must independently match session/root identity to its
acknowledged input and native terminal evidence; this capture does not prove
provider completion. A single successful adapter terminal delivery followed by
the invocation returning closes the post window. A missing or duplicate terminal,
exception or cancellation leaves coverage incomplete. `snapshot()` retains
partial observations after failure; a harness cannot be reused for a second turn.

`events` contains immutable, ordered send/edit/delete observations, unique
capture evidence IDs, actual serialized requests and resulting message payloads.
`messages` contains surviving messages in Discord post order, with exact content,
embed text, component labels, attachment names, message IDs and observation times.
Edits replace only supplied fields; deletion removes the message from the final
snapshot. Drafts remain in history but do not reappear in finalized text. Footers
and cards remain separate from message content so the consumer can select its
explicit predicate domain instead of treating a headless return as a Discord post.

`post_capture_complete` covers this accepting fake gateway's message boundary.
`text_capture_complete` additionally refuses complete coverage when a surviving
attachment could contain unobserved text. Table PNGs are captured as files, never
converted into fabricated visible strings. Neither flag certifies browser pixels,
real gateway timing, permissions, webhook identity, reactions, tool posts outside
this gateway, or the full bot ingress/admission path. The default bot transport
path is exercised; identity webhooks are explicitly disabled. Configure the
lifecycle's table, notification, cancel-view and unprompted options to match the
scenario before comparing its output. Account balance footers require the host's
DB context and are outside this standalone harness. No product defaults change.

N9 binds its `VisibleText` from these observations only after matching the owned
capture window and identity. Keep predicates depending on uncaptured domains
PENDING. Native outcomes and headless receipts remain independent evidence.

## Owned database session factories

Call `daimon.testing.db.require_owned_sessionmaker(factory_or_engine)` before
opening a test-hook session. It validates the main bind and every routed
`binds` value, including connection binds' engines, without connecting or
changing the factory. Only local PostgreSQL `daimon_test_nc_*` lane databases
are allowed. The `daimon_test` exception requires both `GITHUB_ACTIONS=true`
and `CI=true`, plus `build_test_engine` provenance for a current-process
`test_w<pid>_<8hex>` or `test_f<pid>_<8hex>` schema on every engine. Execution
options and engine clones do not establish that provenance. Unbound factories,
foreign or synchronous binds, URL overrides and malformed routing maps fail
before I/O.

Guard adopters refuse plain local `daimon_test` by design. Their real-database
policy tests skip that documented contributor setup outside GitHub Actions CI;
use an owned lane database to exercise those tests locally.

Shared database fixtures also require the session-scoped `db_preflight` supplied
by the testing package's `pytest11` plugin, keyed by the resolved `db_schema`
value. Re-exported or overridden schemas receive separate admission
even when multiple packages run in one worker. It checks the DSN and process-owned
schema before `db_engine`, `db_nullpool_engine`, `db_clean` or session setup.
Each constructed engine then passes the same registered-schema ownership guard
before connecting. Valid lane/CI fixtures retain their pools, schema isolation,
cleanup and transactions. Plain local `daimon_test` skips before engine creation;
other foreign targets fail before I/O. Existing conftest imports need no edits.
Install the package/plugin with the usual `uv sync --all-extras --all-packages`;
when plugin autoload is disabled, pass `-p daimon.testing.pytest_plugin` explicitly.
Database-free tests do not request the preflight or need a database URL.

Aliased or re-exported `db_engine` fixtures reuse the pooled engine within the
same pytest session when the admitted database URL and worker schema match.
The last fixture user drains background work, drops the schema and disposes the
engine. Different databases or schemas retain separate engines; the NullPool
fixture retains its own pool on the shared schema.

# Pooled committing engine

`db_committing_engine` is a pooled engine (pool 5 plus 10 overflow, as in
production) on the worker schema, for tests whose sessions really `COMMIT` or
that need several independent connections. It replaces `db_nullpool_engine`,
which opens a new connection, about 20 ms, for every checkout. Statements stay
prepared on a warm connection, and code that gates on its pool
(`session_preparation_gate.pool_headroom`) takes the production path. The
pool is session-scoped (`db_committing_pool`); the per-test fixture adds the
checks below. `DAIMON_TEST_NULLPOOL=1` makes the pool a NullPool again, which
restores the old behaviour.

Keep `db_nullpool_engine` for a test that:

- runs `asyncio.run` per call, such as a Typer command under `CliRunner`:
  asyncpg connections belong to the loop that opened them;
- takes a session-level advisory lock on purpose, or kills a backend with
  `pg_terminate_backend`: only closing the connection ends either.

Code that disposes the engine it is handed would replace the shared pool, and the
teardown fails the test for it. Give that code
`sibling_nullpool_engine(db_committing_engine)`, a throwaway NullPool engine on
the same schema.

## What a test can and cannot leak

Pooled connections outlive the test, so the fixture enforces the isolation a
fresh connection used to give. The pool rolls every connection back when it is
returned (`pool_reset_on_return="rollback"`), and refuses to hand out one that
is still inside a transaction. Every connection starts a session at the worker
schema's `search_path` with a 60 s `lock_timeout`.

Rolled back by the pool, so a test cannot leak it: uncommitted rows, an open
transaction on a connection it returned, `SET LOCAL`.

Detected at teardown, which then fails the test and resets the connection so
the next test is not hit as well. For every connection the test touched:

- `search_path` other than the worker schema;
- a session-level `SET` of `statement_timeout`, `lock_timeout`,
  `idle_in_transaction_session_timeout`, `TimeZone`, `DateStyle`, `role`, the
  default transaction isolation or read-only mode, `synchronous_commit`,
  `work_mem`, `client_min_messages` or `application_name`, compared with what a
  brand-new connection of the pool reports (the list is `_WATCHED_GUCS` in
  `daimon/testing/db.py`; add a setting there if a test needs another);
- a temp table or other temp object;
- a `LISTEN` channel;
- a session-level advisory lock;
- a connection still checked out, with or without a transaction;
- a session fence (`pool_headroom`) permit that was never released, or a
  replaced pool.

Every pooled engine the fixtures build (`db_engine` and the committing pool)
also fails the test that returns a connection to its pool inside an open
transaction, naming the test and the backend pid, and terminates that
connection. Without this, such a connection keeps its row and advisory locks,
the next `COMMIT` on it is only a `RELEASE SAVEPOINT`, and the sibling pooled
connection's wipe waits on those locks until the job times out. The usual
cause is several tasks sharing one `AsyncConnection` (a test body plus a
background task on `db_session_factory`): `daimon/testing/db.py` makes COMMIT and
ROLLBACK on such a connection wait for the statement running on it, which is
what lets a rollback and a new transaction interleave otherwise.

Not checked: a `SET` of a setting outside that list, prepared statements a test
creates with SQL `PREPARE`, and changes
a test commits to `public` or to another schema. A test that needs any of these
should use `db_nullpool_engine`, whose connections close with the test.

# Reusable schema for `fresh_schema` tests

A test marked `@pytest.mark.fresh_schema` runs its own DDL, so it cannot use the
worker's shared schema. It gets the worker's one reusable DDL schema
(`test_f<pid>_<8hex>`), built from the ORM models on first use and dropped with
the worker's other schemas. `db_session` runs the test in a transaction and rolls
it back, and Postgres rolls DDL back with it, so a test that only issues
`ALTER`, `CREATE` or `DROP` and never commits leaves nothing behind.

A test that commits anyway (`session.commit()`, `engine.begin()`) is caught
before the next one starts. The next `fresh_schema` test compares a catalog
fingerprint of the schema with the one taken after the build. The fingerprint
covers columns (type, nullability, default, identity, generation, collation),
indexes, constraints, triggers, functions, views, sequences, and enum and domain
types. If it is unchanged, the test deletes every row and restarts the
sequences that were advanced; if it differs, the schema is dropped and rebuilt.
Either way each test starts on an empty, ORM-shaped schema, as a private one
would give it.

`DAIMON_TEST_FRESH_SCHEMA=1` restores a private schema per test (CREATE,
`create_all`, DROP around the test), which is slower and isolates the same way.

# Isolated provider SDK clients

`mux.testing.isolated_sdk` supplies `build_async_openai`,
`build_async_anthropic` and `build_gemini_client` for tests. Each requires an
explicit HTTPX transport and supplies a fake API key, a fixed provider base URL
and empty custom headers by default. Override `api_key`, `base_url` and `headers`
explicitly when a test needs a different wire shape. HTTP clients use
`trust_env=False`; Gemini also receives explicit SSL contexts. Install the
corresponding OpenAI or Google GenAI SDK alongside this package to use those
factories; imports remain lazy.

```python
import httpx
from mux.testing.isolated_sdk import build_async_openai

transport = httpx.MockTransport(
    lambda request: httpx.Response(200, json={"data": [], "object": "list"})
)
async with build_async_openai(transport=transport) as client:
    models = await client.models.list()
```

Construction raises `AmbientSDKConfigurationError` if any variable in
`SDK_ENV_VARS` is present, even with an empty value and even for another provider.
Every factory additionally refuses unknown `ANTHROPIC_`/`OPENAI_`/`GOOGLE_`/`GEMINI_`
variable names (case insensitive), including empty or future settings; unknown
names and all values are omitted from errors. The same check guards late OpenAI
and Anthropic SDK copies. Checks iterate names before set membership,
avoiding `os.environ` membership's implicit value lookup. Both `GOOGLE_API_KEY`
and `GEMINI_API_KEY` are refused before Gemini construction, even with an explicit
test key, because Google GenAI 2.7 reads them unconditionally. This includes
provider keys, webhook/admin keys, org/project settings, custom headers, base
URLs, Anthropic identity/configuration and Gemini credential,
Vertex and replay settings. Errors name variables without exposing values. The
factories never clear the process environment; explicit test keys prevent
credential-chain fallback. Start tests in a clean environment or remove only
the relevant variables with a scoped test fixture. Missing keys, routes or
transports also fail before HTTP client construction. Generic proxy and certificate environment settings are
refused before construction. The names-only guard additionally rejects
`SSL_CERT_FILE`, `SSL_CERT_DIR`, `SSLKEYLOGFILE`, `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, `NO_PROXY`, `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE`, including
empty, lowercase and mixed-case variants. `trust_env=False` alone does not
disable Python's `SSLKEYLOGFILE`; Gemini constructs its explicit SSL context
only after this preflight, with no key-log filename. Existing owned clients
retain their explicit TLS/HTTP configuration if ambient settings appear later;
new OpenAI/Anthropic SDK copies repeat the admission guard.

Close OpenAI and Anthropic clients with their normal async context manager.
Gemini owns both clients: call `client.close()` and `await client.aio.aclose()`.
`MockTransport` supplies both transport protocols; a different sync Gemini
transport requires a separate `async_transport`. OpenAI and Anthropic use SDK subclasses
whose constructor guard also applies to
`copy()` and `with_options()`. Late ambient settings therefore refuse the copy
before SDK initialization; existing clients and caller-owned environments remain
unchanged. Clean copies retain the SDK's arguments, owned HTTP clients and wire
shape. New factories default to one HTTP attempt, including Gemini models and
interactions. Anthropic accepts an explicit `max_retries` and `timeout` for tests
exercising retry behavior.

The existing `build_fake_anthropic`, `build_stub_anthropic`,
`build_no_retry_anthropic` and `ScriptedTransport.client` inherit the isolation guard. Their test key, provider
URL, SDK timeout and retry behavior are preserved: fake/stub use two retries;
scripted defaults to zero and honors its explicit retry budget. The no-retry
builder accepts either a handler or `MARouter` and keeps zero retries, including
409/429/5xx errors. Its SDK copies retain that budget and the isolation guard.

## Owned database session factories

Call `daimon.testing.db.require_owned_sessionmaker(factory_or_engine)` before
opening a test-hook session. It validates the main bind and every routed
`binds` value, including connection binds' engines, without connecting or
changing the factory. Only local PostgreSQL `daimon_test_nc_*` lane databases
are allowed. The `daimon_test` exception requires both `GITHUB_ACTIONS=true`
and `CI=true`, plus `build_test_engine` provenance for a current-process
`test_w<pid>_<8hex>` or `test_f<pid>_<8hex>` schema on every engine. Execution
options and engine clones do not establish that provenance. Unbound factories,
foreign or synchronous binds, URL overrides and malformed routing maps fail
before I/O. Shared fixture setup also runs the preflight described above.

Offline admin proof fixtures skip a contributor's plain local `daimon_test`
outside exact GitHub Actions CI, with a clear lane-database/isolation reason.
The shared admission guard remains strict: invalid CI provenance and routed
foreign engines are errors, rather than skipped authorization failures.

# Offline platform surface capture

`SlackSurfaceHarness` and `TeamsSurfaceHarness` run an invocation against the
unchanged platform `TurnLifecycle`. They capture the adapter's actual outgoing
SDK payloads in-process, without Slack or Teams credentials or network calls.
This observes adapter posts, rather than a provider answer or headless return.
Use the repository environment (`uv sync --frozen --all-packages --all-extras`);
the harnesses require their respective installed adapter packages.

```python
from daimon.testing.slack_surface import SlackSurfaceHarness

capture = await SlackSurfaceHarness(
    evidence_id="qa:turn:1",
    session_id=acknowledged_session,
    root_turn_id=acknowledged_root,
).run(invoke_real_turn)
```

Use `TeamsSurfaceHarness` for Teams. The invocation receives a real lifecycle;
call `post_initial()` as the host does, and then run the actual turn driver with
that lifecycle. The callback return is ignored. Harness options select the real
adapter's notification, table, identity, summary, and host-balance configuration.
Balance reads require an explicitly supplied sessionmaker and tenant.

Slack uses real `AsyncWebClient` methods to serialize Block Kit objects, then a
fake `api_call` accepts only owned `chat.postMessage`, `chat.update` and
`chat.delete` calls. Teams uses the actual SDK `MessageActivityInput` objects and
`TimedSender` through an injectable fake `TeamsSender`. Unknown operations,
foreign destinations and unowned edits fail closed. These boundaries never
fall through to a real platform client.

`PlatformSurfaceCapture` is an immutable JSON-roundtrippable receipt with
identity, monotonic window bounds, ordered post events and surviving messages.
Each event preserves the serialized request separately from the accepted
message payload. Edits retain message order, delete events retire messages, and
history remains unchanged. Receipts freeze when the invocation exits, including
when it raises or is cancelled. The harness accepts one invocation only.

`post_capture_complete` requires one observed adapter terminal hook, a normally
returned invocation, and no delivery failures. It does not assert successful
root completion: a real error card can finish a post window. Swallowed send,
control or retirement failures also make the window incomplete; partial posts
remain available. `text_capture_complete` additionally requires no opaque visual
in any observed state, including a draft later replaced by an answer.

Message observations keep `body_texts`, `card_texts`, `control_labels` and
`fallback_text` separate. Slack markdown blocks retain full displayed text even
when the notification fallback is clipped or escaped; Slack native table cells
are recorded in delivery order. Teams adaptive-card text and controls retain
SDK wire aliases, and plain answer edits remove the old card. Accessibility and
notification fallbacks must not be flattened as an extra displayed answer.
Images, file attachments and unsupported visual nodes leave incomplete text
coverage. Captured strings are posted text leaves, not browser pixels or OCR.

A QA consumer must independently match the receipt to acknowledged native
session/root and host window evidence before binding `VisibleText`. Preserve
transient event text for absence/preamble assertions; final surviving messages
alone cannot establish what was visible earlier. Control labels and card text
are visible leaves, but the receipt does not certify card finalization, tool or
approval effects, admission, platform ingress, authentication or live delivery.
No QA cell becomes PASS just because a harness returned a receipt.

# Isolated provider SDK clients

Pinned OpenAI and Gemini provider replays use the shared isolated SDK factories,
with explicit offline transports and zero retries. Ambient provider settings
are refused before replay dispatch; injected clients do not inherit proxy or
TLS configuration. Request matching and source/version pins remain unchanged.

Catalog lifecycle wrappers still need the separately owned normalized-event
tap forwarding before native preview/draft completeness can be certified.
Keep those dependent checks PENDING; final lifecycle posts and native-root
completion receipts do not establish that additional coverage.
scripted defaults to zero and honors its explicit retry budget. The no-retry
builder accepts either a handler or `MARouter` and keeps zero retries, including
409/429/5xx errors. Its SDK copies retain that budget and the isolation guard.

## Metered channel budget proofs

`daimon.testing.channel_accounting.ChannelAccounting(sessions, scope, platform,
channel_id)` supplies an explicit owned-database accounting seam. Its
`snapshot(now=...)` reads channel ledger entries, usage projections, net spend
and the current production budget status in one repeatable read-only transaction.
All-time channel spend and the budget's windowed spend are separate. Every bind
must pass the shared owned/isolated-CI guard and use the same engine.

`await run_turn(deps=..., prepared=..., pricing=ControlledTurnPricing(...),
thread_id=..., external_user_id=..., user_message=..., lifecycle=..., cancel=...,
operation_key=..., binding=...)` drives the actual `run_prepared_turn` and turn
driver. Preparation, admission, isolated offline provider transports and actual
post capture belong to the caller. The seam binds the real legacy/provider usage
recorder to explicit **mock** prices instead of trusting the preparation's
recorder callback. It inserts no ledger or usage rows directly. Native providers
require their current durable binding and unchanged native usage; missing actual
infrastructure retains pending usage. It supports one root, without delegation
or recovery. The receipt retains the real host outcome, before/after independent
snapshots, new debit rows, pricing policy and explicit issues.
The success flag requires a completed single attempt; a failed or cancelled
turn can still carry genuine reconciled provider charges in its readback.
It correlates the run's observed meter with its exact persisted observation,
native root, revision and debit key. Another root's real charge or an ambiguous
native meter does not certify this run. Native revisions are included in the
same read-only snapshot, scoped through the channel's durable binding slots.

The proof uses USD 100 per million input/output tokens, zero explicitly
inapplicable offline native infrastructure and markup 1. A mocked 100-input /
50-output turn generates one genuine USD 0.015000 ledger debit through production
billing, exhausting the original USD 0.01 total channel budget. These exaggerated
rates exercise host arithmetic; they are not current provider prices or live
spend. Anthropic runs an isolated SDK HTTP/SSE fixture. OpenAI/Gemini accounting
proofs mock only the TurnIO boundary and use their real usage normalizers; this
does not certify their native codecs. The API alone proves neither first-turn
posted text nor the next admission refusal/no-send. Consumers retain those
independent NEW15 checks and do not promote source cells from a helper PASS.

## Host admission evidence

`daimon.testing.admission_evidence.admission_edge` observes one owned attempt.
Pass the actual `TurnDeps`, authorized tenant `Scope`, backend label, platform,
channel/thread/caller identity, capture ID and an `invoke(observer)` coroutine.
Install that observer on core `admit(..., observer=observer)` inside the invocation
and run the real adapter refusal handler. Core emits only the actual admitted
decision or typed denial; its default observer is `None`. An ordinary observer
exception preserves the host decision and produces an opt-in failure warning.

Supply `effects()` with independently captured `AdmissionEffects` (actual
`VisibleText`, `LogObservation`, reactions and coverage), a monotonic injectable
clock, and an independently observed cumulative `native_send_count()` covering
session creation and model input sends. Existing agent/environment metadata reads
are distinct from native work. Unknown counts return `None`; missing counts,
adapter coverage or a core decision remain typed PENDING. The hook checks every
database bind before invoking anything, validates identity and capture windows,
and records positive native-send deltas rather than hiding violated denials.

Receipts contain no provider session/root or completed-turn evidence. Backend
labels do not certify provider execution, live timing or a full catalog scenario.
Capture a silent refusal as empty notices/reactions; never render a notice or
manufacture a log from its reason. Discord's writers-none path is log-only, and
its ingress prefilter may return before core admission. A metered first turn,
ledger join and executable administrative setup require separate observations.
This hook neither mutates budgets/credit nor binds the source executor itself.

```python
import httpx
from mux.testing.isolated_sdk import build_async_openai

transport = httpx.MockTransport(
    lambda request: httpx.Response(200, json={"data": [], "object": "list"})
)
async with build_async_openai(transport=transport) as client:
    models = await client.models.list()
```

Construction raises `AmbientSDKConfigurationError` if any variable in
`SDK_ENV_VARS` is present, even with an empty value and even for another provider.
Gemini additionally refuses every `GOOGLE_`/`GEMINI_` variable name (case
insensitive), including empty or unknown future settings; unknown names and all
values are omitted from errors. Checks iterate names before set membership,
avoiding `os.environ` membership's implicit value lookup. Both `GOOGLE_API_KEY`
and `GEMINI_API_KEY` are refused before Gemini construction, even with an explicit
test key, because Google GenAI 2.7 reads them unconditionally. This includes
provider keys, webhook/admin keys, org/project settings, custom headers, base
URLs, Anthropic identity/configuration and Gemini credential,
Vertex and replay settings. Errors name variables without exposing values. The
factories never clear the process environment; explicit test keys prevent
credential-chain fallback. Start tests in a clean environment or remove only
the relevant variables with a scoped test fixture. Missing keys, routes or
transports also fail before HTTP client construction. Generic proxy and certificate environment settings are
ignored by the supplied clients.

The existing `build_fake_anthropic`, `build_stub_anthropic` and
`ScriptedTransport.client` inherit the isolation guard. Their test key, provider
URL, SDK timeout and retry behavior are preserved: fake/stub use two retries;
scripted defaults to zero and honors its explicit retry budget. The existing
`build_no_retry_anthropic` remains a separate builder outside this change.

## Owned database session factories

Call `daimon.testing.db.require_owned_sessionmaker(factory_or_engine)` before
opening a test-hook session. It validates the main bind and every routed
`binds` value, including connection binds' engines, without connecting or
changing the factory. Only local PostgreSQL `daimon_test_nc_*` lane databases
are allowed. The `daimon_test` exception requires both `GITHUB_ACTIONS=true`
and `CI=true`, plus `build_test_engine` provenance for a current-process
`test_w<pid>_<8hex>` or `test_f<pid>_<8hex>` schema on every engine. Execution
options and engine clones do not establish that provenance. Unbound factories,
foreign or synchronous binds, URL overrides and malformed routing maps fail
before I/O. Ordinary fixtures retain their existing behavior.

For native artifact bytes, real adapter upload capture, and conservative PDF
checks, see [output ownership observations](OUTPUT-OWNERSHIP.md). Synthetic
protocol controls never certify model generation or a catalog PASS.
## Read-only turn accounting

`daimon.testing.turn_accounting.TurnAccountingReader` reads Anthropic, OpenAI
and Gemini usage from an owned test database. Construct it with `sessions`, an
authorized tenant `scope`, the current `binding`, `native_session` and
`root_turn_id`. It checks every database bind before connecting and uses one
`REPEATABLE READ READ ONLY` snapshot. It neither claims outbox rows nor changes
usage, prices or ledger entries. The public store seam is
`daimon.core.stores.turn_usage.read_accounting_revisions(session, tenant_id=..., binding=...)`;
callers of that lower-level function own authorization and snapshot consistency.
The store suppresses autoflush without changing the caller's pending objects or
session policy; even an ordinary caller-owned transaction remains a read here.

```python
report = await reader.snapshot(
    attributions=tuple(revision_rates),  # RevisionRateAttribution per exact id/revision
    inventory=native_inventory,         # TurnUsageInventory captured independently
    markup=Decimal("1"),
)
```

Every `NormalizedUsageRevision` retains the original observation, native revision
and meter, latest/selected flags, optional settlement state, attributed model,
dated `ProviderPrice`, evidence references, token cost, infrastructure dollars
and seconds, total cost and signed ledger delta. A missing native model needs
independent admitted model context and `model_evidence_id`; a tariff alone does
not establish attribution. Missing rates, evidence, token stages or container
time remain unknown. `not_applicable` infrastructure requires explicit zero
dollars and an evidence reference; its duration stays `None`. Rates are supplied
explicitly because accounting revisions do not persist the applier's tariff.

`TurnUsageInventory` binds the binding/session/root to every latest native
id/revision and the evidence for a terminal inventory or exhausted request walk.
Capture it from the native path, never by enumerating these accounting rows.
It may attribute historical request spans lacking a persisted turn ID without
rewriting them. Missing members, stale revisions, other unattributed session
work, pending settlement and mismatched ledger cost prevent `complete=True`.
Disjoint child usage adds its own model work; independently measured shared
infrastructure must be attributed once. Covered aggregates and session-to-turn
allocations remain explicitly unsupported.

The report separates `coverage_complete`, `pricing_complete` and
`settlement_complete`. `total_usd` and `expected_ledger_usd` are `None` until
coverage and prices are complete; a known ledger debit can still be reported
while those totals remain unknown. `known_model_usd` is only the subtotal of
known selected model costs, including an empty subtotal of zero. All monetary
aggregation uses decimal precision 80. Controlled rates and inventory evidence
prove offline host arithmetic; they do not certify live invoices or supply a
missing posted footer. `issues` explain incomplete results.



## Metered channel budget proofs

`daimon.testing.channel_accounting.ChannelAccounting(sessions, scope, platform,
channel_id)` supplies an explicit owned-database accounting seam. Its
`snapshot(now=...)` reads channel ledger entries, usage projections, net spend
and the current production budget status in one repeatable read-only transaction.
All-time channel spend and the budget's windowed spend are separate. Every bind
must pass the shared owned/isolated-CI guard and use the same engine.

`await run_turn(deps=..., prepared=..., pricing=ControlledTurnPricing(...),
thread_id=..., external_user_id=..., user_message=..., lifecycle=..., cancel=...,
operation_key=..., binding=...)` drives the actual `run_prepared_turn` and turn
driver. Preparation, admission, isolated offline provider transports and actual
post capture belong to the caller. The seam binds the real legacy/provider usage
recorder to explicit **mock** prices instead of trusting the preparation's
recorder callback. It inserts no ledger or usage rows directly. Native providers
require their current durable binding and unchanged native usage; missing actual
infrastructure retains pending usage. It supports one root, without delegation
or recovery. The receipt retains the real host outcome, before/after independent
snapshots, new debit rows, pricing policy and explicit issues.
The success flag requires a completed single attempt; a failed or cancelled
turn can still carry genuine reconciled provider charges in its readback.
It correlates the run's observed meter with its exact persisted observation,
native root, revision and debit key. Another root's real charge or an ambiguous
native meter does not certify this run. Native revisions are included in the
same read-only snapshot, scoped through the channel's durable binding slots.

The proof uses USD 100 per million input/output tokens, zero explicitly
inapplicable offline native infrastructure and markup 1. A mocked 100-input /
50-output turn generates one genuine USD 0.015000 ledger debit through production
billing, exhausting the original USD 0.01 total channel budget. These exaggerated
rates exercise host arithmetic; they are not current provider prices or live
spend. Anthropic runs an isolated SDK HTTP/SSE fixture. OpenAI/Gemini accounting
proofs mock only the TurnIO boundary and use their real usage normalizers; this
does not certify their native codecs. The API alone proves neither first-turn
posted text nor the next admission refusal/no-send. Consumers retain those
independent NEW15 checks and do not promote source cells from a helper PASS.
## Host termination evidence

`daimon.testing.turn_conformance` exports `TerminationCase`,
`TerminationSnapshot`, `TerminationEvidence` and async `termination_evidence`.
Pass an owned `async_sessionmaker`, authorized `scope`, `platform`, an independent
`required_reasons` sequence and named `cases`. Each case declares `expected`,
`session` (or None before native creation), an async normal host `run` callback,
optional `expected_error_class` and `expected_recovered`. The callback returns
the actual `RunOutcome` from `run_prepared_turn` or terminal lifecycle `TurnState`.
For a raised recovery exception, retain the actual lifecycle state if testing
that specific reason; a bare exception only supplies its generic mapper fact.

The typed result contains the independent reason oracle and snapshots of actual
state/exception and committed reason, error class, session, account, tenant,
platform and recovery fields. The collector never seeds expected identity into
its observation or adopts a matching old outcome row. Missing committed rows
and unterminated states stay missing evidence. Every bind is checked with
`require_owned_sessionmaker` before execution. Provider owners must review complete
reachable-reason coverage and attach optional hooks explicitly. An absent hook
is PENDING; partial collector controls never certify C18 or a live provider.

## Opt-in product QA settings

`QAProductProfile("opt-in-product").flags` declares exactly four settings: core
`mcp.require_routine_destination`, `discord.credential_form_expiry`,
`discord.writers_none_notice` and `turn.native_footer_cost`. Applying explicit copies combines settings in the same
section without consulting credentials or constructing settings. Missing product
declarations refuse with `ProductProfileUnavailable`; the report remains PENDING.
The default profile keeps all four disabled. An application receipt supplies no
source, native-send, card, accounting or user-visible outcome evidence.

The explicit `python -m daimon.testing.a7_anthropic --mode live` and
`python -m daimon.testing.f2_anthropic --mode live` entries require exact signed
financial authorization, a separate pinned lead GO, clean merged source,
installed config/ledger receipts, reviewed recipe and authorized disposable
bindings. Missing admission facts produce PENDING before credentials or a hold.
A7 runs one legacy and one mux turn in two fresh sessions sequentially. F2
uses the public default-capability flow with eleven authored skills, builtins
and an explicitly bound provider-reachable MCP endpoint; loopback fixtures
cannot supply live MCP evidence. Each session has one attempt, SDK retries zero,
and an exclusive envelope marker prevents a second run at a different output.

The A7 entry remains guard-only. The F2 entry additionally requires the
documented native session budget through SDK `extra_body`: `type=limit`,
`max_list_cost.amount="25"` (25 US cents). Creation acknowledgement and an
independent session readback must echo that exact budget before any user message.
F2 requires a fresh financial authorization and trusted GO digest pinning the
budget and native tool configuration; old guard-only F2 authorizations refuse.
Its isolated SDK instance disables builtin and MCP toolsets by default, enables
exactly bash/read/edit/grep/glob/write and describe_agent/list_my_sessions, and
explicitly disables web search/fetch. Effective native readback is checked,
including default policies and absent delegation. Product SDKs and schemas do
not change. Token thresholds remain observed facts, never provider token caps.
A7's total deadline is 300 seconds and streamed stop estimate $0.28; F2's
are 600 seconds and $0.52. Fifteen seconds within that same deadline are
reserved for interruption, archive and close. The stop uses the signed dated
Haiku 5.5 price table, disjoint model spans and a separately labelled wall-time
estimate. It cannot prevent unseen in-flight charges. Final missing runtime,
TTL splits or counters retain the whole $0.35/$0.65 hold for owner reconciliation;
no wall time or rounded native list cost is presented as exact billed spend.
Only acknowledged created IDs enter cleanup. Normalized F2 recordings use the
existing recorder; raw provider bodies and credentials are never exported.
The default modes remain offline and do not load credentials or a ledger.

The live monitor consumes the SDK's decoded SSE events before forwarding each
frame, including fragmented CRLF/JSON and multiple events in a single chunk.
Conflicting verified walks keep the maximum dated request floor and any earlier
financial token-limit latch through the public settlement edges; they never
certify complete actual usage. A create acknowledgement received after stopping
or sealing is quarantined outside the accepted inventory. Its durable orphan
receipt triggers scoped single-attempt retirement (session interrupt/archive),
with no new session or second settlement. This bounded late cleanup is explicitly
outside the expired run deadline and retains the hold for reconciliation.

Live usage identities are keyed by the accepted session plus native request ID.
Cross-session native ID collisions are quarantined and exported with both session
provenances for reconciliation. Financial proof projections keep one original
maximum-cost witness per collided native ID and independent maximum-token witnesses;
they cannot count a collision twice, fabricate a native ID, or erase a verified
floor or overrun latch. Actual usage and runtime remain incomplete and held.

The live probe transport monitors every native stream endpoint through the SDK
decoder regardless of Content-Type and fences every response after settlement.
Opened streams are force-closed; delayed responses are closed and refused.
An adversarial already-pending iterator that supplies usage after settlement
creates a private 0600 `.n4-late-usage-*` sidecar beside the ledger. It preserves
accepted session/request/model proof, the pinned conservative monetary floor,
and the retained unknown hold, with admission-blocked and reconciliation flags.
Both `admit` and `run_live` refuse A7 or F2 while any such ledger sidecar exists,
before an attempt, reservation, credential read or client construction. N8 must
reconcile and archive that evidence outside its blocking namespace before F2 GO;
a caller boolean cannot clear it. There is no second settlement or ledger edit.
Cleanup bookkeeping, interrupt/archive, retirement, SDK close and the single
settlement are protected by a shielded finalizer within the original deadline.
Repeated caller cancellations are absorbed until that finalizer finishes, then
cancellation is re-raised. Financial witness ranking uses the exact pinned N8
monetary bound; missing cache TTL never removes its conservative retained hold.

The A7 replacement entry pins financial authorization
`5dc7a801d7566e63e91fd9e30f2c667db613166ab27eedd1d629af616950b9a1`;
the original attempt marker and hold remain untouched. A separate source/binding
GO is still required. Managed-agent spans normally expose aggregate cache writes
without TTL splits. The stop monitor prices such writes conservatively at the
pinned highest one-hour rate, while complete splits retain exact pricing. This
estimate neither invents TTL counters nor changes unknown actual usage or guard
settlement. Receipt request metadata uses fixed route templates and separately
audited authorized resource IDs. Unknown routes and credential-bearing identities
refuse export; the shared recording audit is unchanged.

A7 retains each audited completed leg in a private `a7-legacy-leg.json` or
`a7-mux-leg.json` before checking its send claim or later usage/cleanup proof.
The final receipt preserves these partial legs on failure. Successful claims
include both accepted and processed; pending/uncertain/failed claims still refuse.
This captures final text and outcome without certifying a failed run as PASS.
R3 is a new signed attempt; prior markers, holds and receipts remain unchanged.

Late usage is audited against every registered credential, including F2 MCP
bearers. Proof enrichment also refuses unknown/string-bearing meter metadata;
credential halves in separate frames cannot become durable provider notes. The
initial admission latch survives audit refusal. Thread `/stream` endpoints and
other identified SSE responses use the same monitored SDK decoder; undecodable
streams refuse.

`python -m daimon.testing.f2_budget_acceptance` is a separate signed-only,
create-only acceptance probe. It needs `--authorization`, its independently
trusted `--authorization-sha256`, `--go-receipt`/`--go-sha256`, `--live-binding`,
`--budget-config`, `--spend-ledger` and fresh `--output`. Its separate
$0.02/60-second envelope is pinned to the lead-signed acceptance digest below. The binding
must name an existing explicitly authorized disposable Haiku5.5 F2 agent with the
exact effective allowlist. It creates one budgeted session, verifies independent
readback, archives, and sends no user message. Its transport refuses events and
thread/model work. Acceptance is not an empirical enforcement test, and absent
billed runtime remains unknown/held rather than an invented exact zero. This
probe must never consume/reuse the F2 workload authorization or attempt marker.

Worst-cost GO evidence uses official 1M input/128k output limits, the highest
cache-write price and the routing basis. Without verified global routing, N8's
conservative US-rate bounds are $2.92 A7 and $1.72 budgeted F2 including running
time. F2 follows A7 reconciliation and needs at least $2.22 real cap room including
a $0.50 margin, no admission latch and no unresolved late evidence. These bounds
are conditional on the documented single-thread/request controls and billed
runtime limits; local clocks do not prove successful provider termination.

### Closed late usage identities

The opt-in live runner closes provider identity and free-text fields structurally.
Only native shapes `(sevt|sesn)_[A-Za-z0-9]{20,40}` are eligible for late numeric
proof enrichment, and a repeated event identity closes further enrichment.
Rejected IDs persist only SHA256 plus `shape_rejected` / `duplicate_rejected`
metadata beside the trusted 0600 reconciliation latch. Even eligible unique IDs
are hashed in `proof.id` and `proof.session.id`, preventing alphanumeric credential
halves from masquerading as different shape-valid IDs. The original native IDs
remain private in memory; N8 can match hashes against its authorized inventory.

Final and orphan display receipts also hash native IDs and provider free text,
including request/thread references, threshold IDs and result evidence. The host's
status vocabulary and numeric evidence remain readable. These display projections
do not alter canonical BudgetGuard settlement inputs or the original one settlement.
Full Audit remains defense in depth: original late frames and result/meter values
are checked, and every projected durable field remains in cumulative literal
credential scanning. Only exact host-generated hashes and host-owned config/ledger
pins bypass recursive encoded-text interpretation. A hit or a full 256-record /
1 MiB scope closes enrichment; the unknown actual, retained hold and admission
latch remain, including after cancellation. Interleaved, reversed and base64-split
native-ID regressions exercise the real SDK decoder with a mock transport.
No product or adapter defaults change, and no additional live run is authorized.


F2-R2 financial admission is pinned to the lead-signed digest
`6103a70419f4d1260c97eb0f3be60a3e238701cd9de7ae5ccd802f894b1b6a18`.
The independent acceptance envelope is pinned to
`8628b05e1b6d637b152c138a0977dc806e40833e9fc6e1d54794881a9ae063a3`.
Both require the exact native budget and effective tool allowlist. The old
`8eea963b` guard-only authorization and caller-rewritten replacements are refused.
Each still needs its own separately signed source/binding GO and exclusive attempt
marker. Financial signatures authorize neither execution nor a retry by themselves.
No acceptance or workload is run by composing this entry.
`FooterAccountingHook.evaluate_turn` binds this reader to actual accepted Discord
footer payloads. Supply `capture`, an authoritative `terminal`, independently
normalized native `receipts`, per-revision `attributions`, an independent native
`inventory`, and `markup`. It compares receipts against latest durable revisions;
historical cumulative revisions are not added as extra work. A sealed request
walk can establish complete Anthropic request-span coverage without inventing
an aggregate observation. Unknown rates, container time, incomplete inventory or
unsettled outbox leave checks PENDING. A known ledger debit is never an unknown
provider total. The original `evaluate` path remains available.

Complete controlled accounting returns `ledger_matches_usage` PASS. The footer
check additionally requires an actual accepted `used` footer matching the queried
ledger debit. OpenAI/Gemini currently omit that field, so their posted-cost check
remains PENDING. Independent inventory, rates and native receipts must come from
the admitted execution path; never enumerate the DB being checked to mint them.
Offline control receipts prove host arithmetic only, not catalog execution or
live spend. C12 aggregate certification remains cut.

## Structural post-history predicates

`daimon.testing.post_structure.post_history_from_capture` converts a real
`DiscordSurfaceHarness` receipt into the optional `TurnEvidence.post_history`,
retaining every accepted send, edit, delete and final survivor. Supply the actual
`PostRootAcknowledgement` independently; the helper never invents a provider
root, input ID, terminal record or coverage. Ordinary turns omit the field.

The outcome oracle accepts `message_count` with explicit `min`/`max` bounds,
`fences_balanced`, `chunks_gap_max_s` with a nonnegative `max`,
`footer_on_last_message`, and `card_edits_min` with `min` and `during: running`.
All require a complete, acknowledged history matched to an authoritative
completed root. Missing evidence stays PENDING; invalid histories fail.

Answer counts use surviving messages with nonempty raw content; status embeds
and deleted drafts do not count as answer chunks. Fence checks inspect every
answer chunk separately, matching line-fence delimiters and lengths. Chunk gaps
use the first accepted observation of each final chunk's exact body, rather than
the earlier status-card creation time or a later footer edit. Text predicates
stay PENDING when attachments leave text coverage incomplete. Footer position
requires a raw embed footer only on the final answer message; flattened hints
and footer-looking body text cannot certify it.

Running card edits join actual accepted edit events to complete progress-card
observations by message identity and timestamp. A sufficient number inside
complete native tool start/end intervals proves a lower bound during running.
Missing or insufficient intervals stay PENDING; an injected clock or a
progress-looking card alone does not prove native execution. Fewer eligible
edits across complete post/card captures fails the minimum. Sends and final
footer edits do not count.

These are adapter-boundary predicates. They do not certify native completion,
real Discord delivery, chunk-reply ingress or whole catalog scenarios. The
NEW10 chunk-2 reply and D6 long-running source cells remain PENDING until their
actual ingress and native evidence are independently bound.

The credential-expiry clock proofs admit their configured database before
requesting engine, schema-sweep or cleanup fixtures. Plain contributor-local
`daimon_test` skips before setup; foreign destinations fail before connection.
Registered isolated GitHub Actions CI schemas retain the shared admission rule.

### D11 across actual platform posts

`FooterAccountingHook.evaluate_turn` accepts the original Discord capture or a
`PlatformSurfaceCapture` from the restored Slack/Teams harnesses. Bind the same
session/root/platform/destination as the independent usage inventory. The reader
retains native revision versions and accepted post receipt IDs; it reads the
owned ledger/outbox through `TurnAccountingReader` without writes. Slack cost is
read only from posted context blocks; Teams only from the visible small summary
TextBlock. Answer text, notification fallbacks and controls cannot supply it.

Provide complete `RevisionRateAttribution` entries, with independently dated
controlled rates and explicit infrastructure evidence, plus the native inventory
seal and terminal. Missing rates, duration, totals, settlement or capture remain
PENDING with their typed reason. Once those prerequisites are complete, the
frozen D11 requirement for a posted cost executes: no cost is **FAIL /
POSTED_COST_MISSING**, rather than PENDING. The unchanged native OpenAI/Gemini
adapters currently omit cost, so their controlled D11 outcome is FAIL even when
independent host arithmetic passes. Anthropic's controlled priced-footer proof
passes. These component controls certify neither live prices nor complete source
scenario ingress. No source assertion or aggregate usage row is manufactured.

Accounting fixtures admit the configured destination before requesting any engine,
sweep or cleanup fixture. Plain local `daimon_test` skips; owned lane databases and
registered isolated GitHub Actions schemas retain the shared guard policy.

The native caller regression also executes N9's actual OpenAI/Gemini SDK and host
path, consumes its accepted post capture and recorded terminal, and independently
normalizes the served native usage snapshot. The private case keeps the frozen
D11 assertions verbatim, uses an owned UUID tenant, and pins Gemini's codec to
reviewed merged #838; frozen source files and pins remain untouched. Complete
controlled rate/infrastructure inventories settle the same native revision through
host accounting. These proofs report the observed native cost omission as FAIL,
with no PASS promotion or claim about live rates, spend or source ingress.


The opt-in product profile includes `turn.native_footer_cost`; default reports
never include `PASS under opt-in profile` in default counts. Native footer tests
record normalized usage during actual SDK/host execution, use the existing
bounded final usage walk, capture real Discord/Slack/Teams posts and independently
query the owned ledger/outbox. Complete controlled usage and prices prove host
arithmetic only; they do not certify live spend or source ingress. Missing
inventory, rates, infrastructure or settlement remains pending in accounting;
a complete posted capture without the frozen required dollar field is an
executed failure. Other unavailable opt-in declarations still refuse the full
product profile; a D11 component control does not certify those other factories.

Tests replaying signed financial envelopes or dated rate fixtures must pin the
clock to the fixture's validity window. The Anthropic live-entry tests use a
fixture-scoped 2026-10-11 clock; admission expiry and stale-price negative cases
advance that clock or pass an explicit expired timestamp. Monotonic deadlines,
immutable signed JSON and the runtime expiry checks remain unchanged.

`AdminBindings` executes owned writers rules, budget set/clear, positive tenant
credits and turn-cap overrides through real product APIs, with independent
readback. Credits retain the CLI note/request-id identity and replay behavior;
invalid frozen negative credit setup remains typed pending. Host admission and
Discord permission-denial evidence are separate hooks. See
`tests/judge/ADMIN-BINDINGS.md` for literal command shapes and the per-cell gaps.

## Anthropic catalog admin observations (C6)

Explicitly import `daimon.testing.anthropic_admin.AnthropicAdminBindings` and
assign an instance to `AdminBindings.resource_edge`. Supply a `CliRuntime` sharing
the binding's isolated SDK client, owned sessionmaker and deployment defaults,
with Discord, Slack and public MCP integrations disabled. Creation accepts only
an explicitly supplied `AgentFixture` manifest of files beneath `fixture_root`,
with exact SHA256 hashes. No shell, environment discovery or backend conversion
is involved. Other providers and unsupported shapes return typed pending receipts.

The edge runs the existing Anthropic `agents create/get/archive` and
`channels rule set ... --readers own --copy-from ...` CLI functions. Fork and
archive receipts come from independent service/SDK reads. `readback_cli()` accepts
the literal agent-get assertion command; `AgentReadback.cli_text` captures the default CLI table and
`AgentReadback.cli_json` preserves its native JSON, including custom skill IDs. A copied own skill receives a new native
ID; no names are substituted to satisfy source assertions. An intervening revision
change between CLI listing and SDK retrieval refuses the observation.

For D5, call `attempt_member_edit(MemberAgentEdit(member_scope, fixture_name))`
with a real owned local platform member account and matching principal. It runs
the actual MCP description update and server attachment with the native identity
selected by independent readback. Receipts retain full before/after payloads,
revisions and actual refusal errors. An error does not establish unchanged state.
After main #505, the unbound-agent probe observes both edits refused with the
admin handoff. Independent SDK/CLI readback keeps revision 1 and the payload
unchanged. This observation changes no product permissions and does not assert
a completed model turn or a whole-cell pass.

Whole D14/D5 cells still require the original runner assertions and actual host
turn/log/text evidence where authored. Omitted callbacks stay pending. A1 native
create conversion remains unsupported by this Anthropic-only edge.

The composed resource admin proofs use the shared database preflight before
engine/session setup. Plain contributor databases skip before allocation;
owned lane and registered isolated-CI schemas retain the real service/SDK
readback paths. This composition adds no local ownership policy or fixture
shortcut and leaves the signed resource operations unchanged.

## Metered NEW15 admission join

`daimon.testing.new15_bindings.New15Bindings` joins the original frozen channel
budget scenario to an owned offline host. Supply `AdminBindings`, its exact
`TurnDeps`, the actual member `Scope`, channel/thread/user identity, an explicit
`ControlledTurnPricing`, a first-turn `DiscordSurfaceHarness` factory, and the
second attempt's real adapter invocation, passive observer, platform effects
and cumulative native-work counter. Its `run(source, evidence_id=...)` checks the
pinned original source bytes and owned collaborators before mutation.

The join calls real `admit`, public `bind_session`, and
`ChannelAccounting.run_turn`; callers never construct an admission or prepared
turn. Native runtimes, SDK clients and HTTP fixtures are explicitly supplied.
Use the shared `isolated_sdk` builders and owned `MockTransport` boundaries.
The original literal USD0.01 total budget is set before the first turn. The same
real setter is repeated after the actual debit, before the second admission;
no zero budget, direct ledger insertion or synthetic exhaustion is used.

The first Discord lifecycle captures actual serialized posts. Its root metadata
is joined only to a single completed record in the actual PostgreSQL native
journal; absent or ambiguous terminals stay pending. The budget denial uses the
unchanged real adapter refusal handler and `admission_edge`, including the
source's twenty-second observation window. Its receipt has no native turn or
session identity. Separate read-only accounting snapshots query the genuine
channel debit and exceeded budget, then confirm budget cleanup without removing
the debit. The `New15Run.outcome` property recalculates the pinned authored
checks; a copied PASS or matching caller-supplied hash pair is never consulted.
Foreign capture identities, mismatched roots, missing usage/coverage, extra
native sends and silence cannot earn a pass.

The twenty-second window is authored by the source; what is observed depends on
the `clock`/`sleep` pair of `New15Bindings`. By default both are real time, so
the window is a genuine wall-clock observation. The included tests inject the
event loop's own clock instead: they patch `loop.time`, stamp every capture
(posts, logs, the admission window) from it, and advance it in 0.25 s steps with a
real slice of I/O between steps. Timers the system armed (`asyncio.sleep`,
`loop.call_later`) therefore fire at their due loop time inside the window, in
order, and a late answer is captured. The window is covered in loop time, not in
wall time. Work that only wall time can advance is not covered, so a task the
denial spawned that is still running after the window, or a thread it started,
fails the capture rather than going unseen. An injected pair must keep that
property: it may not add to the clock without running the timers due before it.

The included actual-SDK/Postgres proofs exercise Anthropic, OpenAI and Gemini.
Their explicitly **mock** tariff is USD100 per million input/output tokens,
markup one and zero offline native infrastructure; 100 input plus 50 output
produces USD0.015 of test ledger debit. This is neither live price evidence nor
provider spend. Gemini's driver record store is same-process only. Catalog-wide
assertions, real Discord gateway timing and durable/restart certification remain
separate evidence; the scoped authored outcome does not certify those domains.

New DB proofs request the shared `db_preflight` before session setup. For a stale
local editable installation, set `PYTEST_PLUGINS=daimon.testing.pytest_plugin`
until its pytest entry point is refreshed; the environment setting also reaches
the immutable golden runner's child processes. No production SDK builder or
pytest runner source needs changing for this local installation detail.

NEW15 outcomes revalidate current evidence at decode time: complete adapter and
native-send coverage, the authored 20-second observation, finite effect timestamps
inside the capture window, the 15-second notice deadline, and matching receipt /
decision identity joined to the first Discord thread and persisted usage caller.
Stored pending gaps cannot certify an incomplete or altered capture. Late answers
are still checked across the full observation window. Contradictory identities or
out-of-window effects refuse; missing coverage/duration remains typed pending.

Gemini's C6 operator profile is explicit: `opt_in_gemini_operator_profile` installs
`GeminiCliResourceEdge` over an enabled native A1 catalog adapter and the same
owned `AdminBindings` sessionmaker. Its immutable CLI grants bind tenant,
account, channel, current config digest and provider workspace; native copy/get
validate the returned host before catalog/provider access. Missing opt-in keeps
the default paths unchanged. An installed Gemini factory leaves unconfigured
neighboring channels and tenants on the existing dispatch; a configured Gemini
channel still requires its exact current grant. Mixed-runtime controls compare
the existing Anthropic agent-get SDK request bytes and output with factory-off.
The edge runs the literal channel-copy and agent-get
CLI functions and returns their actual table output. Independent native catalog
readback cannot replace the frozen CLI assertion: D14 currently fails its two
skill/model checks because the table omits those fields, despite retained copied
skill bytes/model. Native archive and D5's authored Claude YAML conversion remain
explicitly pending; no member edit or native turn is manufactured. All proofs
are offline actual SDK HTTP requests with owned Postgres readback.


## Conformance and manual probe tooling

`mux.testing.conformance` owns the backend-neutral conformance runner,
normalized recorder and live-probe BudgetGuard. Provider SDK/script adapters live
under `mux.testing.drivers`; platform conformance callbacks live under
`daimon.testing.adapters`. These are test-only dependencies and are never
imported by runtime packages. The former `mux.conformance` namespace is removed.

The conformance kit, its tests, saved receipts, replay tapes and reference docs
now live in `packages/mux` (`mux.testing`, `packages/mux/tests`,
`packages/mux/docs`), so the mux suite runs without Daimon installed.
Product runtime usage/outbox reconciliation continues to use the mux state
ledger; the manual admin hold-reconciliation tool is test support only.

CI uses the full import-linter contract for the product/test dependency policy
and `scripts/lint_distribution_boundaries.py` for configured package roots and
built-wheel file placement. No source import scanner duplicates that policy.
Product source may not import conformance/testing modules, and no distribution
may include `test_*.py` or
`conftest.py` inside its package. Product wheels exclude QA harnesses, receipts,
probe budget examples and mux package documentation. Testing helpers remain in
the separate development-only testing distribution; the tests/data/docs are
outside its package.

### H6 staging receipt and request boundary

`ProbePlan.h6` binds the exact `H6-ANTHROPIC`, `H6-OPENAI` or `H6-GEMINI`
cell to an attempt/leg, verified source/deployment/GO/envelope/quote/scope
references, the installed config/ledger, an aware deadline, a reviewed cash
bound and exact HTTPS request routes. A cell's target is independent of the
provider charged for predecessor, admin, setup, cleanup and smoke calls.
Ordinary plan, spend and tape serialization is unchanged when these fields are absent.

Pass an explicit underlying transport to `run_probe`, and give the SDK's
HTTPX client `transport=recorder`. `Recorder` rechecks the existing reservation
before forwarding, records redacted request metadata, and refuses duplicate
writes, unknown routes, expired/changed/released bindings and exhausted request
inventory. Merely calling `record()` after I/O is insufficient. Unknown spend
retains the reviewed bound and blocks another leg; failed legs consume the attempt.

`H6Receipt` validates five native/visible turn joins, unchanged follow-up
bindings, fresh-thread identities, pinned models/footers and exact reply-marker
hashes, with every closed receipt-reference family. The caller's `verify_h6`
artifact reader must independently validate the real preview/admin/CAS/audit,
setting/revert, native/delivery, usage and cleanup artifacts and signed manifest.
It raises on missing or foreign proof and returns `None`, never a boolean approval.
Absent proof, reader or known actual accounting produces typed PENDING.
For these staging legs, token estimates alone retain the full reserved cash
bound (or a larger proven overrun); only complete measured spend releases savings.

These offline contracts grant no authority and earn no staging points. The live
entry still needs the exact lead SIGN/PIN, fresh N8 quote/native bounds, all
deployment and tenant bindings and real artifact capture. Native HTTP request
counts do not bound Managed Agents internal inference, and this local wrapper
does not automatically attach to a remotely deployed Discord adapter or smoke.
Those actual request edges must be bound before a live run. No new SDK clients,
credential loaders, ledger, staging runner or product hooks are installed.

Fleet outcome captures persist `FleetWorkerFacts.from_worker(worker)` through
the existing `FleetWorkerCapture.worker` field. The projection omits private
specs and arbitrary contract maps, retains original result pointers, and keeps
only independently validated review facts with explicit coverage. An invalid
sibling field never erases a valid finding. Unsupported review data stays
unverified without hiding a known failure. Recorded results accept unchanged
normalized text payloads and exclude opaque native records/references; a
private or binary payload is refused. Genuine in-memory worker/event inputs
are projected at the boundary; serialized raw worker maps are rejected.

### Scoped fleet evidence export

`python -m daimon.testing.fleet_export capture --tenant-id UUID --run-id UUID
--attempt-id ATTEMPT --source-sha FULL_SHA --output NEW_DIRECTORY` reads only
that persisted fleet through the existing core store APIs. The operator supplies
`DAIMON_DATABASE__URL` through the container's normal configuration; no DSN,
provider key or admin authority is accepted on the command line. Run it only
under an authorized export envelope. The database transaction is read-only,
repeatable read, with a statement timeout and a bounded event inventory.
The DB reader reuses the stores' authoritative column reads for runs, bindings,
projections and journal events. Effective dirty/deleted source rows refuse
before that source is read; pending, unrelated and reverted caller state stays
unflushed and unchanged. Cached ORM entities never supply exported row facts.

The fixed `export.json` nests the oracle's safe worker facts. Arbitrary worker
specs and native bodies are excluded. Normalized events share the recorder's
fixed schema boundary, with tool inputs replaced by its omission sentinel.
Original result payloads must survive unchanged and pass the existing core
result validator. Reused root-session turns stay unbound until an immutable
run-specific input join exists; prior root history is never dumped. Unsafe
required results refuse publication. Unknown event payloads retain explicit
metadata-only omissions. Capture checks exact private
slots, accepted bindings, provider/profile/session identities and source versions.
Paired worker facts and persistence rows must agree on both version and error
code before publication and during offline verification, even with fresh hashes.
The credential audit classifies a public review digest only through its typed
reviewer-to-original-result binding, including unchanged review JSON and journal
text. Other free-form hex content remains subject to encoded-content refusal;
raw, joined and decoded credential checks still apply.

Publication creates one fresh 0700 directory containing 0600 `export.json`,
`manifest.json` and `SHA256SUMS`, using a Linux atomic no-replace rename. It
refuses existing destinations, symlinks, unsafe schemas, secrets, source changes,
truncation and malformed result pointers. `python -m daimon.testing.fleet_export
verify DIRECTORY` checks the bundle offline, including scope, closed schemas and
hashes. A hash proves integrity, not authority or successful fleet execution.

This first core exports persistence facts only: native/platform captures, wakes,
worker outcomes and financial execution joins remain explicit PENDING until
owner readers are composed. It fabricates no accepted input, terminal, timing,
financial actual or scenario verdict. Successful capture/verification exits 3
(PENDING); a refused operation exits 2 with a content-free diagnostic. It cannot
alone authorize dropping required live evidence before a database downgrade.

Fleet probe admission uses the shared staging binding and existing budget guard,
including exact role/model/cash terms and durable dispatch claims. The Gemini
worker term pins `gemini-3.8-flash`; the $0.49 hold and all other cash terms
remain unchanged. Quote runtime-enforced worker caps with `ModelPrice.reserve`
and `TokenLimits`; a signed cash hold alone is not a native token envelope.

The after-ACK `AgentMCPBinding` owner also accepts `AgentMCPAuthority` for other
providers. The selected caller must verify actual native ACK/readback and its
association to the original canonical source. Foreign native ids never become
Daimon agent ids. The owner reuses the existing registered token mint, private
pending-intent receipt, readback and exact-JTI revocation lifecycle. Its legacy
Anthropic LiveContext input retains the same ACK gate and behavior. This is a
provisioning callback, not authorization to allocate sessions or send prompts.
For source-preserving Gemini full mode select `chat_execution=True` on the
explicit authority. It uses the same registered mint and revocation owner but
signs `chat_agent_id`, retaining ordinary chat visibility and search collapse.
The default `False` continues to sign `agent_id` and retains the restricted
external agent-chat surface. Full mode refuses that different authority. Both
source catalogue capture and runtime require the exact registered chat authority;
preview credentials are short-lived and revoked immediately after discovery.
A source with an absent/disabled daimon-mcp toolset or a catalogue subset cannot
be made full by the presence of a caller credential.

For a captured shared source, use consent's canonical source reference (transport
service-account ownership is omitted), and verify the caller's captured resource
scope and actual source/counterpart association before admitting the callback.
The logical caller account always comes from the scoped authority, never the
native source transport account.
