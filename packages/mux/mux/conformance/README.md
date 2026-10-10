# Offline conformance runner

Run `uv run pytest packages/mux -n 2`. The existing pytest-mux CI shard
collects this directory. No provider requests, SDK discovery or runtime wiring occur.

Register a driver-specific factory with `Registry.register(name, factory)`;
it returns a fresh `Adapter(ManagedAgents, store, ScriptedTransport)` per probe.
Call `await run(registry, name)` for C01–C18 results. Results carry `pass`,
`fail`, or `pending` and evidence. Pending prevents certification. Exceptions
are reported by class only so upstream exception messages cannot expose secrets.
Fixture-authored `ConformanceFailure` messages identify the failed check; explicit
`require` checks also run under `python -O`. Reference pending and pass IDs are
pinned in CI, so moving a probe from executable to pending fails the matrix.

An adapter seeds native responses in `arrange(Cxx)` and injects upstream faults
in `fault(name)`; it must not edit the driver's projected state or return a verdict.
C02 distinguishes disclosed expiry from unexpected continuity loss.
C02/C09 seed two binary artifacts, the first containing bytes 0–255, and C09
shares a channel vault. An interrupted download may resume with all exact bytes
or return a typed failure; truncated, duplicated or corrupted success is refused.
`Scenario.shared_resources` names the shared vault, and transport
`deleted_resources` logs actual provider deletions independently of receipts. C05 scripts child-first completion, stream disconnect,
overlapping saved/buffered items, paginated history and an unrecoverable gap.
The expected final item is `item` with text `done`; the one root tool result is
`call` with text `result`. Both identities and exact preserved content are checked.
C06 runs a tool at cancel acceptance, ends the stream without terminal evidence,
then supplies an observed interrupted root outcome. Both wait calls use fresh
bounded future deadlines. C08 deletes a required mount,
fails its replacement, then supplies successful reconciliation. Fixture source
contains the complete assertions and named faults.

C11 submits `SKILL.md` with bytes `fixture` inline through `Skills.create`,
then checks the returned full `Skill` record, explicit version,
deployed agent binding and `Skills.retrieve(scope, skill_id)` record.
Pin identity is the skill ID and explicit version; provided sources must agree.
Source and digest metadata may be missing or become enriched between records;
conflicting provided values fail. Digests are opaque and never required.
Transport `skill_uploads` logs the inline bundle actually received upstream,
decoded from the native request, independently of the returned metadata. The
probe requires that log to contain the exact submitted files and bytes, so
correct version pins cannot hide a missing or corrupted upload.
The adapter seeds a matching native skill record and agent binding; missing
versions, bindings or records fail. This uses the core upload/record API,
without inventing a downloadable bundle or archive field on `Skill`.
MCP and repository bindings remain required, and an unavailable action must
stay explicit. Optional collections set to `None` provide no binding evidence.
Reference pagination honors omitted limits and the `has_more`/cursor contract.

The reference driver is an in-memory, partial, test-only oracle for the runner.
It is not a provider and its passes certify no backend. `IS_TEST_ORACLE = True`
marks the reference module; register it with a name containing `reference`. Unexercised ports refuse
explicitly. Thirteen executable probes cover C02–C11, C13, C15 and C16.
C10 checks driver admission, extension version and cross-tenant rejection. The remaining
five probes have explicit dependency reasons: C01 needs the host's multi-human
queue/attribution and workspace binding, C12 the historical billing bridge,
C14 the driver registry and existing/new-thread selection, C17 the host wake
generation adapter, and C18 the host outcome-row mapping.
They must be implemented against those seams before any C01–C18 certificate.

The runner uses `mux.state.store.StateStore`; the reference factory supplies a
fresh `MemoryStateStore` per probe. A missing store leaves C03/C04/C07/C13 pending.
Never interpret a skip as a pass or remove its reason. State-backed scenarios use
the deterministic store clock `2026-10-09T00:00:00Z` and five-minute leases; a
future database adapter must control its transaction clock for expiry probes.
No Postgres implementation or host integration is certified by the memory oracle.

- C03 races same-key sends, reconstructs a receipt after restart, rejects changed
  content and another principal, and retains unknown delivery after a timeout
  following upstream acceptance. Retries must not add upstream effects.
  The memory store does not suspend inside its transactions, so its default
  sends serialize. A checked-in yielding wrapper suspends between intent and
  claim transactions, proves three claim contenders overlap, and rejects a
  read/yield/write claim that omits the atomic compare-and-swap. Driver adapters
  must provide such suspension to exercise their races.
- C04 injects deaths before/after send-claim and acceptance commits, then before/
  after the journal commit. Intent, event content, projection and cursor survive
  together; replay deduplicates and a superseded worker cannot claim, advance or
  append. A committed send claim requires reconciliation even when no I/O is seen.
  Lease takeover preserves prior uncertain/accepted intents and never permits a
  blind resend of them. Missing and foreign-slot leases are refused; an append
  cannot establish ownership of a session no persisted binding names, even
  after an earlier refused append. Same-thread leases from another account
  are foreign too. Recovery accepts accepted-only acknowledgements, immediate
  processed commits, and native reconciliation after takeover. Queued and
  processed receipts require matching committed input identity, supported by
  the pre-replay record or a native response read during that replay. A write
  made by the replay cannot manufacture its own evidence. The never-sent claim
  stays unknown; a queued receipt without acceptance evidence fails.
- C07 takes normalized usage null → 100 → 120 → 110 through the actual store,
  preserving signed deltas, binding ownership, prior revisions and outbox rows.
  Replays add nothing. A higher same-count revision then requires zero delta
  against revision 4, proving stale replay did not rewind the latest state.
  Each outbox row applies once and final units are 110.
  Host pricing and overlapping-grain billing stay outside this probe (C12 pending).
- C13 races two distinct candidates for an unbound slot through `bind_new_slot`;
  both must adopt the one persisted winner, which survives restart.

The scripted transport's `restart_store(store, crash=...)` reopens the same committed
data and reattaches the driver's ports. Crash plans name a StateStore transaction
and `before_commit` or `after_commit`; the adapter raises `SimulatedCrash` at that
boundary. Upstream records/effects survive separately from local state. C03/C04/C07
seed the scenario's binding; C13 seeds an unbound slot. The reference routes only
C03/C04 sends through durable intent and claiming; its other ports remain partial.
Broken send/store variants and the valid/pending matrix run under normal and
optimized Python. They must fail on fixture diagnostics, not incidental exceptions.

`upstream_sends` records actual scripted acceptance responses by idempotency key,
before local receipt persistence. `reconciled_sends` records native responses
actually read during recovery. Both contain `SendEvidence` and remain independent
of the driver's operation records and returned receipts. C04 snapshots the
pre-replay operation and reconciliation log, then checks the post-replay record
for consistency; only prior durable acknowledgement or a new matching native
observation supports acknowledged recovery. Already-durable processed receipts
remain valid. The reference's `reconcile_send` reads its retained upstream log,
records that observation, and returns no receipt for a key never sent.

## Adapter-declared pending fixtures

Driver factories can declare gaps with `Adapter(driver, store, transport,
pending={"C09": PendingReason(PendingKind.CAPABILITY_UNAVAILABLE,
"provider exposes no native vault API")})`. Import `PendingReason` and
`PendingKind` from `mux.conformance`. Kinds are `capability_unavailable`,
`live_key_required` and `adapter_dependency`; the detail must be a nonempty,
adapter-authored explanation, not a provider exception message. Pin each
declaration in the driver's matrix tests. The map is snapshotted and immutable;
invalid fixture IDs or untyped reasons are refused. Declarations defer only
their named fixture before its arrangement/port calls. All 18 result IDs remain
visible in registry runs. Other fixtures still exercise the actual driver;
undeclared `UnsupportedCapability` exceptions remain failures.

`Result.pending_reason` retains the typed declaration alongside readable
evidence. Existing host-pending reasons remain unchanged. A PENDING result never
certifies, and a typed pending reason cannot accompany PASS. Use
`await run_fixture("Cxx", adapter)` for a single probe with the same policy.
A live-key reason is an explicit adapter declaration, never automatic credential
discovery. This runner API has no recorder or live-budget dependency.

## Standalone manual probe budget

`mux.conformance.budget` exposes `BudgetGuard`, `ProbePlan`, `TokenLimits`,
`TokenUsage`, `SpendReceipt`, `BudgetRefused` and `BudgetLedgerError`. The module
imports no recorder or SDK and performs no provider I/O. A caller needs separate
lead authorization for live work. `live-budget.example.json` has empty model
maps; unknown models refuse admission. Supply reviewed USD-per-million input,
cached-input, cache-write and output rates that bound every billable operation,
and enforce the declared token limits across setup, retries and cleanup.
Unpriced tool/storage fees need separate accounting.

Use the same configured absolute `ledger_path` for every provider/process. The
lead initializes it once with `BudgetGuard.initialize(config_path, spend_path)`;
existing or partial state is never reset. Retain `spend.md`, its checkpoint and
its stable sequence lock together. Missing, empty, malformed, symlinked or
inconsistent state, copied paths and paired ledger/checkpoint rollbacks refuse
admission. Reservations are appended and fsynced under a file lock before any
billable I/O, with a provider-total checkpoint and independently retained sequence
marker. Interrupted updates fail closed and require lead reconciliation.

```python
from pathlib import Path
from mux.conformance.budget import BudgetGuard, ProbePlan, TokenLimits, TokenUsage

guard = BudgetGuard(Path("reviewed-budget.json"), Path("/absolute/sprint/lanes/N9-qa/spend.md"))
plan = ProbePlan(provider="openai", model="reviewed-exact-model", fixture_id="C16",
                 limits=TokenLimits(input_tokens=2000, output_tokens=500))
reservation = guard.reserve(plan)  # Before all billable I/O, including setup.
# An independently authorized caller invokes the provider and measures usage.
# receipt = guard.settle(reservation, status="completed", limits=plan.limits,
#                        usage=TokenUsage(input_tokens=..., output_tokens=...))
# On exceptions/cancellation: settle with status="failed"/"cancelled", no usage.
```

Exactly 80% is admitted; greater prospective provider or total spend is refused.
Completed measured usage releases only known savings. Unknown totals, failures,
cancellations and crashes retain the worst-case reservation. Cached/write inputs
are inclusive subsets; unknown buckets use the highest input rate. Partial usage
can still establish an overrun, which is conservatively charged and blocks even
zero-cost future runs for that provider. Each reservation settles once. Opening
spend cannot be reduced by changing config; removed providers still count toward
the total. Metadata is restricted to identifiers and conservatively audited for
raw/encoded credentials before writing receipts.

Config and all three state files are trusted operator state: deliberately
initializing another ledger or restoring the ledger/checkpoint/lock together is
outside this local guard's boundary. Initialization is an operator convention,
not authentication. Never delete state to retry. The lead reconciles corrupted
state and actual provider spend; the guard has no automatic reset or repair.
Recording, provider adapters and live certification are separate features.

## Normalized conformance recordings

`recording.Recorder` records only request metadata and fixed, validated mux
`Event` contracts. It has no HTTP transport callback, raw response field, SSE
reader or binary response format. Version 1 native tapes are rejected; there is
no migration or fallback. Capture events **after** driver normalization. This
feature exercises normalized contract behavior, not provider HTTP/SSE codecs.

```python
from mux.conformance.recording import Recorder, RequestMetadata

recorder = Recorder()
metadata = RequestMetadata.from_request(
    "POST", "https://probe.invalid/v1/events?key=not-retained",
    headers={"content-type": "application/json", "authorization": "not-retained"},
    body={"input": "body values are never retained"},
)
# normalized_events are actual mux Event objects emitted by the driver.
# recorder.record(metadata, normalized_events)
# recorder.save(path, fixture_id="C16", provider="fake", model="fake", complete=True)
```

Request projection retains method, path without origin/userinfo/query/fragment,
body field names (top-level only), and a closed, redacted header allowlist.
`accept`/`content-type` retain only JSON/SSE media constants; other values become
`[redacted]`. `authorization`, `x-api-key`, and `x-goog-api-key` always retain only
that marker. All other headers disappear. Direct metadata construction enforces
the same allowlist. Body values never enter a tape, including nested objects.

`Event.native.record` and `raw_ref` are removed **before serialization**. Open
`native.*`/`agent.thread.*` event types, native content parts/actions, and inline
binary images are refused, because they could hide opaque response bodies.
Use normalized text or artifact references. These unsupported evidence shapes
need a separate adapter capability/PENDING declaration; they never silently pass.
Tool-use `input` and required-action `payload` mappings retain only the fixed
sentinel `{"input_omitted": true}`. Tool names and required fixed scalar fields
remain; arbitrary argument keys/values are never serialized. Export and replay
require that exact sentinel (boolean true, no extra keys), even after mutation.
Every other payload mapping must be a closed contract DTO; new arbitrary mapping
slots refuse recording. Replay cannot certify tool argument content and adapters
must declare PENDING for checks that require it. Existing free-form-input v2 tapes
are refused rather than rewritten. Other normalized text remains unchanged.

Fixed event payloads, metadata and their string leaves receive the same final
credential audit on export and replay. Actual event text is joined across all
batches with structural IDs/type metadata excluded, including decoded text-leaf
projections. Percent, Unicode, hex, literal escapes and base64 text are audited
with bounded decoding and per-operation memoization. Any `sk-`/`AIza` prefix plus
at least 32 key-alphabet characters is sensitive regardless of its neighbour.
Supplied `secrets=(...)` cover additional opaque credential formats. Auth fields,
Bearer/Basic credentials, surviving key patterns or ambiguous long encoded runs
refuse the entire tape before a temporary file is opened. Normalized text is not
rewritten; false positives require inspected evidence to be re-recorded.

Tapes are created with mode 0600, capped at 4 MiB and never overwrite an existing
path. A rejected batch poisons that recorder so catching an error cannot export a
partial safe-looking run. Incomplete runs cannot be replayed for certification.

`await replay.events(metadata)` returns detached normalized events in recorded
batch order with no network fallback. A caller-provided fake transport builds its
Events port from those events. `await replay_fixture(path, factory)` supplies a
fresh replay to each adapter, runs the Cxx check again and requires full batch
consumption. Changed metadata, extra calls or unconsumed evidence fail; declared
PENDING remains visible and never certifies. Metadata matching deliberately
cannot compare body values or dropped query/header values. Native pagination,
error kinds, raw byte fidelity and codec behavior are outside this tape format.
The C16 reference test re-runs its journal-invariance check and fails when a
recorded normalized event changes; it is no provider certification.

`live_probe.run_probe(guard, plan, path, invoke, secrets=())` is an explicitly
supplied callback wrapper around the standalone budget guard and recorder.
Callbacks enforce token bounds across setup/retries/cleanup and return
`ProbeOutcome(usage, result)`; the result is not stored as evidence. Reservation
precedes callback I/O, settlement precedes export, and failures/cancellation keep
conservative receipts even when recording is refused. No credentials, SDKs or
live calls are discovered or scheduled by this harness.

## F1: default capability scenario

This supplementary scenario is separate from the eighteen standard cases. It
uses the actual `defaults/agents/daimon.yaml` name/system, all eleven skill trees
and six logical builtin capabilities. Only the probe model is overridden;
production defaults are unchanged. The reserved `daimon-mcp` server and matching
MCP toolset are attached explicitly to the disposable agent.

`DefaultManifest` contains eleven `NamedSkill(name, upload)` bundles, the logical
builtin names, one `MCPConnection` and `skill_to_invoke="file-handling"`.
`DefaultCapabilityAdapter` takes the driver, scope, probe model, optional
environment, upstream transport evidence, optional typed pending reason,
`builtin_mapping` and `atomic_revision_pin`. The mapping maps each of
`bash/read/edit/grep/glob/write` to its actual `ToolSpec`; several capabilities
may use the same tool. ToolSpecs are deduplicated before deployment. An omitted
capability is typed CAPABILITY_UNAVAILABLE PENDING before I/O. A custom mapped
tool must have host-executed results; a builtin must have agent-executed results.

Transport evidence exposes exact upstream `skill_uploads`, the actual
`deployed_agent` mapped to a neutral `AgentSpec`, an `agent_spec(agent)` mapper
for provider readbacks, and an optimization-safe `assert_consumed`. These are
facts obtained independently from actual upstream requests/readbacks, never a
verdict inferred from the requested spec. Native settings must retain enabled
and permission semantics in their mapped ToolSpecs.

The runner creates and retrieves eleven distinct skills, requires immutable
version pins, checks exact upstream bytes, creates/retrieves the default agent
and creates one session. With atomic pinning enabled it passes and checks the
agent revision. Without it, it passes `Revision(local=0)` with no native revision
and reports the missing atomic pin as a capability gap, without claiming CAS.
Skill pins are required in both modes.

One scripted turn reads the exact pinned SKILL.md, successfully invokes distinct
`client_context` and `list_events` on `daimon-mcp`, writes/edits/reads `f1.txt`,
greps its edited content, globs its basename and runs bash. Checks reject
failed/unpaired/duplicate calls, wrong routes or servers, missing capabilities,
altered readback, previews, cross-session/root records, gaps, errors and multiple
turns. The first running record must name a nonempty root that matches its own
turn ID before it establishes the stream's root. Loading the pinned instructions
is the skill invocation; scripted output does not establish model quality.

Use `run_default_capability(manifest, adapter, recorder=recorder)` for a scripted
turn, then save a complete F1 tape. Replay uses
`replay_default_capability(path, manifest, factory)` with a fresh fake and
`DefaultCapabilityReplayEvents(replay)` injected into the driver. It reprovisions
against the adapter's offline fake and rechecks normalized results, never a stored
verdict. Logical send/stream metadata are not a native HTTP codec recording.
Free-form arguments stay omitted; replay does not certify their contents.

The committed Anthropic tape is generated only by the scripted real SDK/driver
in `packages/mux/tests/test_default_capability_anthropic.py`. To record a fresh
tape to a NEW path (existing files are refused):

```bash
uv run python packages/mux/tests/test_default_capability_anthropic.py /tmp/f1-new.json
uv run pytest packages/mux/tests/test_default_capability_anthropic.py -n 2
```

No provider call or key is used by these commands. F2 live runs remain separate,
lead-authorized, budgeted work; no live certification is claimed here.
