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
