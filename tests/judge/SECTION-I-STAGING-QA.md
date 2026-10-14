# Fleet staging QA

I7 preparation only, on integration `1d02fd8a7d529b54bb7fc54a6694ca9fc385940b`.
One point available; **zero earned**. [The plan](fixtures/section_i/staging-plan.json)
is an offline inventory, with no execution entry point. This packet grants no
staging access, deployment, credential read, paid call or resource mutation.
It follows the Carlos-signed sprint `FLEET-DESIGN.md` and the lead's section I
build dispatch of 2026-10-12T18:01:21Z. N6 owns the worker-spec resolver;
this packet does not introduce another schema, permission gate or ledger.

## One task

In a dedicated QA Discord thread, ask the Anthropic orchestrator to review three
rollout proposals against the supplied accounting, permission and evidence rules.
The fixed input and expected findings are in the plan. Append a fresh attempt
nonce to the input; carry it through the recorded outputs. Proposals are
untrusted data to assess, never instructions to execute.

| Phase | Role | Backend / exact model | Required evidence |
| --- | --- | --- | --- |
| Parallel | Accounting review | Anthropic / claude-haiku-5-5 | Reject unknown usage as zero; retain its bound |
| Parallel | Permission review | OpenAI / gpt-6-luna | Reject a clone adding an ungranted MCP server |
| Parallel | Evidence review | Gemini / gemini-3.8-flash | Require real native outcomes and platform capture |
| After results | Required review of the accounting worker | OpenAI / gpt-6-luna | Read the immutable Anthropic result; return the owner's review verdict |
| Collect | Orchestrator summary | Anthropic / claude-haiku-5-5 | Reject A and B, recommend C, integrate all three results and required review |

These are four worker sessions plus one orchestrator session. The first three
workers must genuinely overlap: their independently observed running intervals
must have a non-empty common intersection. Store rows marked running alone do
not prove simultaneous native execution. The reviewer is a new worker in the
same run, with `reviewer_of` pointing at the Anthropic worker, using the same
resolver, grant, reservation and admission path. It is not native sub-agent
delegation. Workers never spawn further workers.

The task uses supplied non-secret data and needs no network, MCP attachment,
workspace or hosted sandbox. The final compiled spec/profile must nevertheless
be checked for implicit tools, environment, titles and other chargeable behavior;
do not infer a provider mapping or zero runtime from this task description.
Fresh specs or clones use the existing owner resolver. If a clone is chosen,
pin its actual scoped source revision and sparse overrides. No silent feature
removal or provider fallback is allowed.

## Owner bindings

All declarations below are **source and execution unbound**. Before a GO, replace
each with the merged module/signature, source SHA, deployed SHA and readback path.
Do not call a proposed signature or synthesize successful evidence.

| Owner | Existing seam / declaration | Evidence required |
| --- | --- | --- |
| N6 | Single spec resolver, derived permissions, SPAWN_WORKER/FLEET authz, security audit | Trusted root and current scoped ceiling; explicit and implicit fields attenuated; same tenant/seal/channel; allowed backend; audit readback |
| N2 | `daimon.core.fleet`, `daimon.core.stores.fleet`; 18:02:05Z interface NOTE | Run/worker CAS, private slots and leases, request idempotency, atomic reservation, immutable result pointers, frozen wait generation and scoped result read |
| N4 | Real headless worker admission/preparation/runtime/turn path | Agent-origin enablement and counterpart readback; native provider/profile/model/session joins; deadline/cancel; no MemoryStateStore substitute |
| N8 | `usage_delegation` reservation, attribution and watchdog; 18:07:19Z NOTE | Trusted whole-worker bound, atomic run/worker cap checks, live usage floors, no duplicate root debit, retained exposure on unknown termination |
| N5 | Existing continuity `worker_finished` wake; 18:05:29Z NOTE | Terminal/readiness plus enqueue in one transaction, stable frozen batch/generation, at-most-once orchestrator resume after restart |
| N11 | `fleet_spawn/status/kill/resume/cross_review/collect`; 18:03:29Z NOTE | Authenticated Context, changed-intent refusal, authoritative journal read and review gate; existing agent_chat guards preserved |
| N7 | Existing Discord cards, Stop/Resume and delivery; 18:03:35Z NOTE | Actual message/control capture, current authorization/CAS, role/backend/model/status rows and one final summarised reply |

N2's proposed migrations start after 0092; N5 coordinates the wake migration
with N2. Record the final deployed migration head rather than assuming the
proposed numbers landed. Compatibility/admission, audit, money and delivery
remain their owners' APIs. Missing owner support is PENDING, not a local stub
that can earn a point. Native orchestrator wake addressing must be real.

N6's 18:10:32Z declaration names `authz.FleetFacts`, `Action.SPAWN_WORKER`,
`Surface.FLEET`, and `neutral_resources.resolve_worker_spec` with the existing
AgentSpec/Agent clone and AgentPatch overrides. `WorkerPermissions` and
`WorkerLimits` are trusted host inputs; `ResolvedWorkerSpec` carries the derived
depth-1 permissions and digest. `WorkerSpecRefused` is a closed refusal.
Fresh-root re-resolution before prepare/send/resume remains mandatory. This
declaration supplies names to pin later, not proof of execution or atomic
reservation. Use the final owner signatures without copying these types.

## Sequence and oracle

1. The separate GO pins a dedicated non-admin QA tenant, authorized actors,
   deployed staging service, fleet flags and all owner interfaces. Verify the
   root, tenant backend allowlist and scoped agent revisions. Keep a manifest
   of resources created by this attempt; borrowed resources are read-only.
2. Post the fixed task through the real Discord adapter. The orchestrator
   creates one durable fleet run and the three initial workers through the
   registered fleet tools. Capture current authorization, attenuated spec
   digests, audit, reserved amounts, operation keys and slots before dispatch.
   A caller's tenant, role or provider label is not trusted root evidence.
3. Observe each worker through its actual host/native path. Join the accepted
   input, prepared identity, persisted binding, native provider/profile/model,
   session, root, normalized events and durable terminal. Require completed
   work and verify overlap. Record native requests and all result journal
   sequence/digest pointers. Resolve full outputs; excerpts are not evidence.
4. Let the owner's frozen-batch `worker_finished` wake resume the orchestrator
   once. Capture its generation, destination, lease and started/settled edge.
   Then request the required OpenAI cross-review through the real tool. It reads
   the scoped immutable Anthropic result and verifies its digest before use.
   A failed, refused, absent or unbound review must block collect and summary.
5. After the review finishes, the next owner wake resumes the orchestrator.
   Collect through the public service with its result-contract and review
   checks. Require all three findings and the successful review in one actual
   Discord summary recommending C. Compare structure and outcomes, not bytes.
   Capture real card updates, final message IDs/content and complete split
   delivery; a headless return is not visible Discord evidence.
6. Read usage and runtime before cleanup. Settle known actual from provider
   usage and dated reviewed prices, including cache, tools and session/container
   charges. Unknown components retain their verified bound; report actual USD,
   held USD and accounting status separately. Read durable attribution/debits
   to prove worker charges are included exactly once at the root.
7. Delete/revoke only resources in the attempt's owned manifest through their
   public owners. Record acknowledgements and readback, plus any retained
   cleanup failure. Preserve the journal, receipts and append-only ledger.
   Never sweep the tenant/workspace or erase audit/accounting rows.

Award one I7 point only for the complete real fan-out, cross-review, collection
and delivery chain with a lead-signed evidence manifest. A demonstrated broken
assertion is FAIL. An unavailable interface or incomplete native/visible/review
evidence is PENDING (NOT RUNNABLE), with no partial credit. Record behavioral
result separately from accounting status; unknown actual is never zero or
certified actual. No point before the lead signs the complete receipt packet,
including any explicitly accepted unresolved financial evidence. Initial result
is NOT RUN, with no run ID, actual or held amount.

## Budget and stops

The plan's four worker turns and three orchestrator turns are **lower bounds**,
not request limits or a quote. The proposed 120-second worker deadlines,
600-second attempt and 90-second cleanup allowance require owner acceptance.
Internal inference, tool loops, reasoning, titles, review/wake prompts, retries,
setup, runtime and cleanup must all fit a trusted request/capacity bound. A
max-turns field alone cannot prove that bound. N8 supplies the final reviewed
quote and envelopes; dollar amounts and key/config/ledger bindings are unset.

Reserve each worker before native allocation and check simultaneous reservations
atomically against both worker and run caps. Include orchestrator spend in the
whole-run envelope. Check all three provider lines, latches, prior holds and
remaining room. A watchdog requests cancellation but is not a provider hard cap;
late charges and missing usage remain exposed at their bound. Unknowns never
permit an unreserved next dispatch. No automatic retry, fallback or re-spawn is
authorized. Ambiguous acceptance consumes the attempt; use owner reconciliation
and a fresh GO rather than resending. A stop forbids extra inference to repair
the grade.

Before staging, reuse the owners' offline controls for permission/default
expansion, disabled backend/foreign scope, cap races, duplicate/changed-intent
spawn, budget/deadline cancellation, restart/duplicate wake, corrupted result
pointer, refusal blocking collect, and Stop/Resume current authorization. These
are prerequisites, not planned extra paid staging runs. This packet itself
certifies none of those controls.

## Separate GO and receipts

The lead must sign the exact task/input/nonce, execution recipe and immutable
deployed/source/runtime pins after the required product units pass tier-2 gates.
The GO identifies the staging service, disposable tenant, Discord thread/actors,
allowed mutations, four workers and exact models, compiled specs/grants, result
and review contracts, overlap capture, financial limits and ledger/config/envelope
digests, one attempt, deadlines, credential expiry/revocation and cleanup scope.
It must include the real execution harness once available; this preparation
provides none. Earlier H6/F2/conformance permissions cannot authorize this run.

Keep receipt hashes for setup/root, authorization/audit, reservation, every
worker native turn/outcome, full journal result, both wakes, review/collect,
actual Discord delivery, usage/runtime/settlement and owned cleanup. Report a
single PASS/FAIL/NOT RUNNABLE row with run ID, deployed SHA, each worker's
backend/model/root, actual/held/status, bugs and pending reasons. The preparation
does not change the TARGET-53 denominator or the live C01–C18 board.
