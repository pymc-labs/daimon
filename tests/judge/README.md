# Recorded transcript judge

The headless catalog executor loads OpenAI and Gemini replay consumers only
when executing that selected provider. Anthropic catalog collection and replay
work without those optional host modules, including the standalone script path.
`test_catalog_optional_providers.py` checks both import paths and actual
Anthropic replay with those modules unavailable, in normal and optimized Python.

The [fleet staging QA packet](SECTION-I-STAGING-QA.md) prepares section I7's
three-backend task, cross-review and Discord summary. It is offline inventory
only; execution needs a separate staging GO and signed budget envelopes.

This is a manual quality check, reported separately from C01–C18 conformance.
`tasks.json` pins 12 task prompts, expected goals and critical dimensions;
`rubric.json` pins five 0–2 score dimensions. Grades must cite existing recorded
event IDs. Full task success and all critical dimensions must score 2 to pass.
A core task blocks only if every configured replay repetition fails (default 3).
Incomplete task coverage is not a backend certificate: results are explicitly
scoped to the supplied recordings. CLI/auth/schema errors stop the run without
turning invalid output into a task grade.

Use a Codex ChatGPT subscription login (`auth_mode=chatgpt`); API-key auth is
refused. The script strips API-key environment variables and provider endpoint
overrides, ignores Codex user config, pins `gpt-6.1-sol`, uses a temporary empty
working directory with a read-only sandbox, and requests schema-constrained JSON.
The task is never replayed through a provider API. Transcript payloads are
untrusted evidence, not judge instructions. Never put credentials into recordings.

The installed CLI's `codex exec --help` verifies `--output-schema`,
`--output-last-message`, `--ephemeral`, `--ignore-user-config`, and `--sandbox`.

Run explicitly:

```sh
uv run python tests/judge/harness.py recordings.json --output grades.json --reps 3
DAIMON_JUDGE_RECORDINGS=recordings.json DAIMON_JUDGE_OUTPUT=grades.json \
  uv run pytest tests/judge/test_manual.py -m judge
```

`judge` is excluded by the root default marker expression. Existing default
collection paths are unchanged. Test the harness without a real judge:
`uv run pytest tests/judge/test_harness.py`. All subprocesses there are fake.

Input is a JSON array, one transcript per backend/profile/task:

```json
[{"task_id":"two_turn_files","backend":"anthropic",
  "profile":"anthropic.managed_agents","recorded_at":"2026-10-09T00:00:00Z",
  "events":[{"id":"event-1","turn":1,"speaker":"tool",
  "type":"file.checksum","payload":{"filename":"probe.bin","sha256":"recorded digest"}}]}]
```

This minimal example is insufficient evidence for task success; real recordings
need both turns, tool effects, checksums and terminal observations. Outputs retain
backend/profile, each repetition's scores, pass counts, rubric/task/transcript
hashes, the pinned judge model and grading date. Same-family judge bias for OpenAI
recordings remains subject to the lead's ~20% Opus spot check (QUESTIONS Q30).

`catalog_runner.py` maps an external QA catalog to headless invocation plans,
without executing its commands, HTTP requests, test jobs or provider turns:

```sh
uv run python tests/judge/catalog_runner.py /path/to/catalog \
  --integration-sha <40-character-integration-head> --run-id qa-fixture \
  --output plans.json
```

The catalog supplies `scenarios/*.yaml`, `SCHEMA.md`, `PROPOSED-KINDS.md` and
`TARGET-53.txt`. Exactly 53 distinct frozen targets produce the scored denominator
of 159 across Anthropic, OpenAI and Gemini. The CLI and replay validator enforce
the frozen TARGET-53 SHA256 `fc14eab684b6aa257ca5c01ab113ef154c9847938d263f2739480299c43cc2f4`;
replay also verifies that the ordered target IDs reproduce that digest. Other scenarios are retained as
unscored extras. Missing targets refuse the matrix; an unknown kind or malformed
scenario affects only that scenario. Source, schema, target and attached fixture
hashes pin the inputs for replay. Setup, steps, assertions, human instructions
and teardown remain in the source snapshot. The Discord set-A global assertions
are included separately for every catalog turn.

Each scenario has a portability classification and explicit gaps. Text, terminal
timing, host lifecycle finalization, artifact and trace assertions map to host
observations; platform layout, reactions, UI and manual checklists require an
adapter surface. CLI/SQL/HTTP jobs, administrative fixtures and unknown extensions
require additional bindings. A classification does not certify execution: every
plan has `evidence_status="pending"` until host fixtures and an outcome oracle
are bound. Nothing earns a PASS in this planning slice.

Channel revisions explicitly name the provider/profile. The requested agent
models are Haiku 5.5, Luna and Gemini 3.8 Flash. Anthropic retains `model=None` in
its channel revision because the agent supplies its model; the host fixture must
verify the requested agent model. Non-Anthropic channel admission remains an
explicit integration gap until its host wiring lands, with no provider fallback.
Turn numbering includes context-only messages and every burst text. Mentioned
follow-ups reuse their channel's planned thread; unmentioned replies, burst
concurrency and reply-to-chunk routing retain adapter gaps. Placeholders expand
only from explicitly supplied values, once; no environment variable is read.

Focused verification: `uv run pytest tests/judge/test_catalog_runner.py`.

The matrix report separates the ordinary product profile from an explicit
`opt-in-product` QA profile. `daimon.testing.product_profiles` controls exactly
five settings: MCP
`require_routine_destination`, Discord `credential_form_expiry` / `writers_none_notice` /
`tenant_cap_waiting_card`, and turn `native_footer_cost`. It copies
declared fields without reconstructing settings from the environment, changing
product defaults, enabling table rendering, or constructing clients. Actual QA
fixture factories consume `current_product_profile().apply(settings, service=...)`;
the returned configuration receipt is not an outcome or source assertion.

`run_product_profiles` runs separate default and opt-in fixture callbacks in
one job. Each board retains the same frozen 159-cell matrix. The combined report
keeps default metadata, cells and `scored_counts` at the top level, lists both
profiles within every cell, and exposes `default_board`, `opt_in_board` and
`opt_in_scored_counts`. An opt-in PASS is labelled **PASS under opt-in profile**;
it never increases the default PASS tally. Missing flag declarations are
`PRODUCT_PROFILE_UNAVAILABLE` with no opt-in dispatch. If a callback only selects
a profile without applying all five settings, its board stays unbound/PENDING
and retains actual request/evidence diagnostics. Exceptions after execution starts
propagate instead of being turned into invented empty execution receipts.

The CLI produces both boards in the same invocation. Its opt-in callback uses
the same catalog runner under the selected context; owner-provided product
bindings must consume that context before these source cases can execute.
Absent form/root/tool/HTTP bindings remain PENDING under either profile, and
configuration alone never promotes a catalog cell. Release owners retain the
default-flag decision. Focused verification:
`uv run pytest packages/testing/tests/test_product_profiles.py tests/judge/test_product_profile_reporting.py`.

`headless_executor.py` executes the frozen scored matrix offline, starting with
Anthropic. It always rebuilds plans from catalog files with the frozen target
pin, checks each authored tape's source hash and exact expanded request text,
and passes every original/global assertion to `daimon.testing.outcome_oracle`.

```sh
uv run python tests/judge/headless_executor.py /path/to/catalog \
  --replays tests/judge/fixtures/catalog_anthropic.json \
  --integration-sha <40-character-integration-head> --run-id qa-replay \
  --output results.json
```

The actual core `turn.driver.run_turn(path="mux")` uses the real SDK with a
closed scripted HTTP transport, per-channel backend admission, explicit
`TurnBackendRequest`, and a fenced `TurnPersistence` journal in `MemoryStateStore`.
The fixture's prepared session is fetched and its frozen agent model checked.
Terminals come from journal records belonging to the acknowledged submitted
input, with host lifecycle closure checked separately. Timing includes setup and
closure, conservatively bounding root completion. Every scripted HTTP reply and
SSE record must be consumed; corruption refuses the run, including under Python
`-O`. A mentioned follow-up selects its persisted thread binding and reuses the
session. This executor owns all binding selections in its capture window;
production admission, replacements/recovery, billing and platform delivery need
separate bindings.

Authored tapes prove host protocol replay, not model instruction following or
remote tool/file effects. The initial tapes cover five catalog scenarios and six
turns, including a tool-only completion and a mentioned follow-up. Commands in
provider tool records are never executed locally. Administrative setup, context
fetch, attachments, burst concurrency, waits and special reply routing remain
explicit execution gaps. Original setup/teardown commands are never run, and
no workspace cleanup occurs. Preview/reconnect tapes need a later capture slice.
Without native fixture inputs OpenAI stays `OPENAI_FIXTURE_UNAVAILABLE`;
Gemini stays PENDING until its G2 host binding is available. The OpenAI
fixture consumer below enables explicit G1 execution. The matrix planner still tracks the 30 extras separately;
the executor report scores only the frozen 159 cells.

Results distinguish full-cell status from `catalog_outcome` (all original/global
checks) and `host_outcome` (supplemental completion/session checks). A passing
supplemental check never replaces a missing catalog assertion. With oracle #675,
the initial frozen run has **1 original check PASS and 13 supplemental checks
PASS**, while **all 159 full cells remain PENDING** for uncaptured/unsupported
predicates. Text/card/platform capture will bind to N3's subsequent oracle slices.
All provider calls here use SDK mock transports; no key, database, live provider
or judge is contacted.

Focused verification: `uv run pytest tests/judge/test_headless_executor.py -q -n 2`.

`headless_output.capture_headless_return` awaits an explicitly bound headless
host invocation and records the exact string received by its caller. Receipts
retain a unique evidence ID and monotonic invocation/return clocks, and can be
serialized for offline replay. Failure, cancellation, invalid output or invalid
clocks refuse the receipt. Empty or whitespace-only returns close this boundary
without a visible message. The caller must use the turn's clock and associate
the receipt with its independently captured root/session.

This capture covers only `headless_return`; it does not certify a Discord post,
card, reaction, routing or tool execution. Native text and render/on_state
snapshots must not be substituted for a returned string. The real headless host
test uses scripted SDK HTTP/SSE to verify that pre-tool narration and tool
results do not reach this boundary. Main #615 returns the complete final
response for delivery; no 1000-character truncation applies.
The test also covers an actual tool-only empty return. It exercises the existing
legacy headless host and driver; mux admission/session reuse are separate runner
checks. The helper alone awards no TARGET-53 score; the executor binding below
feeds these returned-output observations to the effects oracle. Neither
boundary certifies platform parity.

Focused verification: `uv run pytest tests/judge/test_headless_output.py`.
Explicit `wait_done.timeout_s` is currently `PENDING / WAIT_DONE_TIMEOUT_UNBOUND`
before any SDK dispatch, for valid and malformed values alike. A timeout-less
`wait_done` joins the already awaited driver turn. The ten-second offline
watchdog protects the fixture process; it does not certify a catalog wait deadline.

The catalog executor now explicitly selects the `headless_return` output
boundary. Its real mux driver invocation returns the final response after the
last tool, preserving the complete final response returned by the production
headless host (main #615).
The executor receives that returned string through `capture_headless_return`
and binds it to the acknowledged journal root/session, checking its minted
evidence ID and monotonic capture window. It accepts no deserialized output
receipt as an execution input. CLI replay rebuilds these observations from the
source-pinned transport, rather than trusting a prior result report.

`headless_outputs` retains the receipts; `rendered_text` is a compatibility view
of their returned strings. For `surface: headless` only, a nonblank actual return contributes one `VisibleText`
at the caller-receipt time. An empty return has complete text capture and no
visible message. A host exception produces no output receipt or text coverage;
a native failed root that the host actually returns from still retains its
returned text and its independent failed root outcome. No pre-tool narration,
tool result or transient render snapshot is promoted into visible evidence.
Only a headless-surface scenario delivers its returned string directly, so
that return closes its text window. For Discord, Slack and Teams scenarios,
headless receipts remain diagnostic output: `texts` stays empty and
`text_capture_complete` stays false until the scenario's actual surface is captured.
Text predicates on those platforms remain PENDING; notices, cards, drafts,
footers, truncation and joined chunks are not observed by a headless return. Card/reaction/log/tool/approval coverage remains false; native
completion and native tool records do not fill those domains.

N3's effects oracle evaluates every original/global predicate against this
headless view. Authored offline tapes can PASS deterministic returned-text
predicates for headless-surface scenarios only; they certify neither model instruction following nor Discord
rendering, human actions or provider/platform parity. Missing execution steps,
explicit wait deadlines and catalog capability gaps still remain PENDING.


Supply `--provider-fixtures packages/testing/fixtures/target53/index.json` to
execute the OpenAI column through merged G1. N3's fixture loader verifies the
frozen target, original catalog bytes and native payload hashes; the consumer
also verifies the selected codec source and public projection. Each channel
persists an explicit Luna revision. The selected, already prepared session is
owned by a thread binding; this slice does not certify resource provisioning.

The consumer supplies exact cold history and session-read replies alongside
N3's native Agents session input/SSE recipe. Every HTTP reply and SSE frame must
be consumed once and every stream closed. Queued sends have no input/root IDs;
the runner checks the persisted accepted send claim, then binds output to the
fresh authoritative journal root. Prior owned roots may appear in follow-up
history but cannot supply current output. An empty scripted Anthropic client
refuses any legacy fallback. Billing is exempt for these offline turns; no
provider usage or billed-spend claim is made.

Unbound setup, attachments, native tool/workspace recipes, completion gates,
explicit wait deadlines and routing hooks refuse dispatch. No external gate is
released early to earn credit. All source predicates and gaps remain in the
report. The first G1 consumer executes four frozen cells / four turns / 24 mock
requests, producing **0 OpenAI PASS / 0 FAIL / 53 PENDING**. Discord text predicates
remain PENDING because no actual Discord output has been captured; raw headless
returns confer no text credit on those cells. Headless-surface microcases test
complete returned-text capture and follow-up isolation separately. Platform delivery/rendering,
remote effects and parity remain uncaptured. The G2 consumer below executes Gemini separately.


Until the native factory pins every SDK option explicitly, the consumer refuses
ambient admin-key, organization, project, webhook-secret or custom-header options
before client construction. This checks environment variable names only and
never reads or logs their values. The explicit dummy SDK key/base URL and strict
mock transport supply the offline boundary; no credential files are read.


With G2 stacked, the same `--provider-fixtures` option executes Gemini through
its registered `gemini.inline_reuse` backend and codec, using an explicit
`gemini-3.8-flash` revision, caller-private runtime and fenced turn persistence.
N3's logical session labels pin virgin local driver-created configuration
records; no provider event, root, usage or terminal is seeded. Cold sessions
consume the exact native POST, SSE and saved GET. Followups retrieve the old
interaction before sending and retain its workspace/previous-interaction chain.
Saved GET status/steps supply authoritative completion; an SSE completion frame
alone cannot PASS. The accepted native receipt and every journal record bind the
fresh root to its selected session. Strict wire consumption also proves stream
closure. An empty Anthropic transport rejects any legacy fallback.

The first Gemini column executes four cells / four turns / 12 mock requests,
with **0 Gemini PASS / 0 FAIL / 53 PENDING** and eight supplemental host PASS
checks. The full matrix remains **0 PASS / 0 FAIL / 159 PENDING**: the authored
turns do not capture the catalog's Discord surface or missing effects. All
original/global assertions and unbound steps remain. A separate headless-surface
two-turn microcase checks returned-text isolation and reuse without changing
any scored scenario. Offline accounting observes one accepted primary dispatch;
it makes no live spend or price claim and refuses fallback attempts. SDK key,
project, location, endpoint and Vertex/enterprise options refuse construction
by environment variable name only. No credential files or live transport are
used. This slice is conditional on G2 merging; the lead restacks it.

Discord text binding now feeds each executed Discord-surface turn through
`DiscordSurfaceHarness` using the actual `DiscordTurnLifecycle`,
`DiscordPostTransport` and discord.py serialization. The capture runs alongside
the real Anthropic/G1/G2 driver lifecycle; it never posts a substituted headless
return. `discord_surfaces` retains the owned adapter receipts. The binding checks
acknowledged native session/root identity, the host window and surviving-message
consistency with the complete send/edit/delete history. `VisibleText` includes
surviving message content, embed text and component labels in post order. Drafts
and deleted messages remain diagnostic history. Long replies retain the adapter's
actual split messages, independently of the complete headless return.

A single completed delivery closes this transport's text window. Partial adapter
delivery and attachment pixels leave coverage incomplete. Billing-dependent text
predicates remain partial because these offline host turns exempt billing and do
not bind account debits/ledger usage. Titles, cards, external tool posts,
reactions, other platform adapters, browser pixels and live model behavior remain
uncaptured. No catalog assertions or fixture/source pins are weakened. The table
scenario exercises `render_tables=False`, the existing ordinary adapter default:
raw pipe rows in the actual posts produce a real oracle FAIL.

Focused verification: `uv run pytest -q -o addopts='' tests/judge/test_discord_catalog_surface.py tests/judge/test_headless_executor.py tests/judge/test_openai_catalog.py tests/judge/test_gemini_catalog.py packages/testing/tests/test_discord_surface.py`.

Catalog baseline reporting retains raw `CellResult.status` and oracle diagnostics,
and adds `reported_status` to distinguish `EXPECTED-FAIL`. Only the reviewed
QA-NEW37 source and its first raw-pipe predicate qualify under the actual default
`render_tables=False` harness. A completed native root and complete real adapter
text capture are required. Extra/global/host failures remain ordinary FAIL;
expected failures never become PASS. `scored_counts` includes a separate
`EXPECTED-FAIL` bucket within the unchanged 159-cell denominator.

The original Anthropic table replay is non-equivalent: it has internal pipes,
no Markdown separator, and Taste rather than the requested Price. Its prior PASS
is withdrawn. Before dispatch, authored Anthropic table controls must provide
three columns / four rows, outer pipes, a separator, and Fruit / Colour-or-Color /
Price headers. An unqualified control is
`PENDING / REPLAY_TABLE_CONTROL_NOT_EQUIVALENT`; the readability oracle is kept
unchanged. Synthetic matched-input controls are unscored. The fixture owner can
supply an equivalent table independently; it then exercises the same observed
expected default-off failure as the OpenAI/Gemini controls. No native content,
catalog predicate, source pin or application default is rewritten for a PASS.

Current-integration matrix refresh retains the frozen 53 × 3 denominator and
all original/global predicates. After the Gemini explicit-tools merge (#708),
the authored native requests explicitly include `tools: []`; missing tools on
an old wire tape still refuse exact equality. The transport safety default is
retained. Only those request fields and their native file/index hashes change;
authored input text, response frames, saved snapshots and other providers stay
unchanged. Source-verified current SDK/host replays produce fresh receipts.
Slack and Teams catalog hosts now forward the same driver lifecycle hooks to
`SlackSurfaceHarness` and `TeamsSurfaceHarness`. `platform_surfaces` retains
immutable SDK post receipts scoped to the independently acknowledged native
session/root and host window. The binding validates the full send/edit/delete
history and surviving snapshots before supplying body text, card text and
control labels to the oracle. Text is observed at capture close in message
creation order for positive predicates. Forbidden-text and no-preamble checks
also inspect the validated transient post history, so later retirement cannot
hide a visible violation. Notification-only fallbacks never supply visible text.

One normal invocation and exactly one terminal adapter delivery close the
post window. Delivery failures, unclosed runs and unknown visuals retain partial
text coverage; billing-dependent predicates remain partial without a real
accounting footer. Cards, tools, approvals, titles, external posts, live ingress
and browser pixels are not certified by this binding. Newly pinned regression
microcases exercise both platforms through Anthropic, OpenAI and Gemini, with
follow-up isolation and the original global predicates retained. Frozen manual
Slack/Teams rows still have no executable steps/assertions and remain PENDING;
this binding does not rewrite them or add full-cell PASS credit.


The ordered catalog step runner records every original operation in
`CellResult.step_receipts`. It binds local channel revisions, real host turns,
owned-root completion waits and actual delays up to 30 seconds. Explicit catalog
deadlines bound the host task and drain cancellation; a shorter 10-second offline
watchdog can refuse sooner. Malformed deadlines cannot authorize dispatch.
Foreign or tentative terminal captures cannot complete a wait.

Unsupported admin/context/routing/attachment/native-schema/concurrency operations
stop dependent work. A completed earlier prefix can provide partial host evidence
while the full cell stays PENDING. Remote tool/workspace effects retain their
typed gap even when scripted native records are consumed. Preserved operation
hooks clear only after an owned step actually runs; unknown hooks remain barriers.
No catalog operation or assertion is removed, and all source mappings are
validated for completeness and order before any channel/turn callback. Anthropic
missing inputs remain PENDING; their pinned replay fixture handoff belongs to N3.

The Anthropic native manifest is an opt-in input to the same catalog executor:

```sh
uv run python tests/judge/headless_executor.py /path/to/frozen/catalog \
  --replays tests/judge/fixtures/catalog_anthropic.json \
  --anthropic-fixtures packages/testing/fixtures/target53/anthropic/index.json \
  --provider-fixtures packages/testing/fixtures/target53/index.json \
  --integration-sha <40-character-integration-head> --run-id native-offline \
  --output native-results.json
```

The selected manifest owns all 53 Anthropic inputs, including empty recipes;
none falls back to the old five replies. The consumer verifies source/codec,
original invocation text, frozen agent model, persisted session slots, every
native SSE record, owned root terminals, request consumption and the mux usage
fold. Native usage is synthetic replay evidence, never billed spend. Text comes
from actual selected adapter captures, not native render narration.

Completion gates remain PENDING until an owned clock/concurrency hook exists.
Recipe-wide missing MCP/Deepwiki effects conservatively block dispatch and retain
their original diagnostic location. Manual triggers, context/routing, assets,
admin operations and catalog capabilities still need their respective bindings.
This path does not infer approvals, tool side effects, files or remote delivery.
The default CLI and `execute_cell` replay path keep their original selection.

Focused verification: `uv run pytest tests/judge/test_anthropic_catalog.py
 tests/judge/test_headless_executor.py tests/judge/test_catalog_steps.py -q -n 2`.

## Ordered admin consumer

`run_steps`, each provider cell executor, and `execute_catalog` accept explicitly
provisioned N6 admin bindings. `execute_catalog(admin_runs=...)` keys each
`AdminStepBinding(binding, values)` by `(scenario_id, provider)`; unknown scored
keys and provider mismatches refuse. No CLI subprocess, operator config or live
provider discovery is available. The caller owns and closes the binding.

Admin invocations retain their source mapping and location. Executed receipts
contain N6's real DB/service readbacks and measured windows; pending/refused
receipts stop subsequent dependencies with their original code. Exact operation
runner hooks clear only after execution. Portability, catalog/effect, long-wait
and ungranted channel-message gaps remain. A later host dispatch after an admin
mutation has `ADMIN_HOST_CONTEXT_UNBOUND`: existing native replay hosts use a
synthetic memory context, while admin operations use the real provisioned DB
scope. No policy/cap/budget/skill effect or full-cell success is claimed until
the owning product path connects those contexts.

D1/D7 first reach the public add/remove API through real native prefixes on all
three providers. Common head ef01727 has no skill resource edge; it returns
`ADMIN_RESOURCE_EDGE_UNBOUND`, never invented success. N6's separate resource
callback can execute Anthropic channel-skill mutations, once its dependency is
composed and an actual owned library skill is supplied. OpenAI/Gemini channel
skills remain unsupported; uploading an agent skill does not substitute for them.
Frozen source/tape predicates are never changed to remove those gaps.


The offline catalog executor accepts an explicit per-cell `ProductContext`
alongside its matching `AdminBindings`. It maps catalog destinations to owned
channels and threads, uses `configure_channel` and the actual database revision,
then calls real admission, `prepare_session_for_turn` with the product session
operations, and `run_prepared_turn`. Its journal and input acknowledgement come
from Postgres, with no memory-store substitution or manually adopted session.
N6's public resource edge supplies D1/D7 skill attachment/removal and readback.

Each Anthropic turn needs a separately source-pinned `ProductTurnRecipe` covering
admission, preparation and the native turn requests. The original native source,
codec, root, session, text, normalized events and usage remain pinned. A missing,
busy, deferred or refused preparation preserves `ADMIN_HOST_CONTEXT_UNBOUND`;
changing an admin policy alone provides no full-cell execution credit. The
current public preparation function does not dispatch provider-native sessions,
so OpenAI/Gemini opt-ins retain that gap before SDK or memory-store allocation.
Without the opt-in, existing catalog replay behavior is unchanged. These are
offline input/host proofs, with no live provider or model-behavior certification.

Optimized catalog refusal checks allow 180 seconds for their child interpreter
to import the SDKs and execute under shared host load. The source turn deadlines
and refusal mutations inside those checks remain independently enforced.

### Product-profile execution factories

`execute_catalog_profiles` accepts an explicit async context-manager factory
`bindings(profile)` yielding `CatalogProductBindings`. It constructs and closes
independent product surfaces inside each selected profile, then executes the same
frozen matrix and unchanged native inputs. Factories apply the supplied profile
to the actual settings passed to product app, ingress, preparation, scheduler and
accounting code. Mutable HTTP observers, admin bindings, product contexts and
credential callbacks cannot be reused across the two runs.

Missing surfaces retain the original typed execution gaps. An applied settings
receipt supplies no native root, delivery, billing or catalog-outcome evidence.
The default tally and `PASS under opt-in profile` tally remain separate. Factory
failures propagate after closing their resources and restoring the profile; they
do not become invented empty successful executions.

`execute_catalog_profiles` constructs each profile's actual bindings inside its
profile context and refuses shared mutable observers, admin bindings or
credential callbacks before the second execution. The CLI uses it for fresh
disposable HTTP apps per profile. Default factories use `profile=None`; opt-in
factories apply the five-switch public declaration. Both frozen matrices retain
source hashes and assertions; unavailable surfaces remain typed PENDING.

## Agent backend staging QA preparation

[The H6 packet](SECTION-H-STAGING-QA.md) defines the per-backend
switch/turn/revert scoring, retained-thread controls, receipt chain and separate
staging GO. Its [offline plan](fixtures/section_h/staging-plan.json) starts with
all owner/financial execution bindings unbound and zero earned points. It is
preparation only and provides no live/staging entry point.
