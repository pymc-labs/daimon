# Gemini C06/C04 live-round runners

`round_runner` is an explicitly invoked, one-attempt runner. It is not registered
as a default backend, adapter or live fixture. Importing it reads no key and
allocates no SDK client. C11/C18 are deferred; the five previously passing cases
cannot be selected. Shared fixture declarations and historical certificates
remain unchanged.

## Admission and financial recipe

The lead supplies **two independent launch pins**: the SHA256 of the financial
`Envelope` JSON and the SHA256 of a separate GO JSON. The GO must bind the case,
envelope hash, exact merged source, config hash, ledger identity, full scope and
absolute fresh output path. Expired, altered, dirty/unmerged, privileged-scope
or unsealed execution refuses before key access. Atomic creation of the pinned
output directory prevents replay/concurrent use of that same GO/output.

The sole model is `gemini-3.8-flash`. No fallback or SDK retry is allowed, including
503. The separate round config contains only Gemini/that model, total and provider
cap $5, and current dated Standard prices; BudgetGuard retains its $4 admission
stop. It must use the separately installed/pinned round ledger; previous sprint
receipts/holds are not reset. One aggregate reservation precedes key access.

The root/lead-approved A7 model-limit method uses:

| Quantity | Per accepted interaction |
| --- | ---: |
| Inclusive input, with cache priced as ordinary input for admission | 1,048,576 |
| Output, including thinking | 65,536 |
| Input / output USD per million through 2026-12-31 | 0.75 / 3.75 |
| Token ceiling | $1.032192 |
| Reviewed tools | `[]` |
| Preview environment compute | $0 |

Sources: [exact model token limits](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash),
[combined thought/output cap](https://ai.google.dev/gemini-api/docs/thinking), and
[Standard introductory rates and Managed agents tool pricing](https://ai.google.dev/gemini-api/docs/pricing).
`agent_config.max_total_tokens=4096` is additional native defense in depth; the
approved admission formula does not treat that soft threshold as a hard cap.
Each SDK POST's actual body must retain the pinned agent/model, explicit empty
tools, no previous interaction, and an empty inline remote environment.

C06 permits **N=1**, maximum $1.032192. C04 permits **N=3**, maximum $3.096576.
Together those maxima are $4.128768. Runs are sequential with settlement between:
if C06 retains its full hold, BudgetGuard refuses C04 under the $4 stop. This is
not permission to weaken the stop or release an unknown hold. Re-review is
required before introductory rates expire; the live runner refuses 2027 onward.

## Scenario evidence

C06 binds the accepted input/root, cancel receipt, driver occupancy and final
authoritative interrupted event to one scoped session. It opens and closes an
actual SDK stream without delivering its frames (controlled client EOF), and
withholds canonical GET dispatch behind an explicit client barrier. No running
or cancelled native snapshot is manufactured. Until the barrier is released,
EOF/requested cancellation retains actual driver occupancy. Only an observed
native cancelled GET releases it. A completed-before-cancel race is FAIL, with
no second attempt.

C04 executes four original claim/ack crash boundaries against one committed
scenario store, with a fresh binding per boundary: Gemini correctly refuses
new intent on an uncertain occupied session. The independent SDK POST/input-byte
hash and native acceptance must agree with durable acknowledgement. Unclaimed
intent needs an explicit send decision; sent/unknown intent keeps typed
`native_lookup_unavailable`/`outcome_unknown`, with no blind resend or invented
lookup. Local journal/projection/cursor death, overlap, successor fence and
stale/missing/foreign-slot/account/orphan refusals retain the original substantive
checks. MemoryStateStore restart is explicitly a controlled local transaction
simulation, not a deployed cross-process durability certificate. Partial hook
success alone does not award the full runner result.

## Receipts and cleanup

Every JSON HTTP response retains the four exact nullable native counters before
SDK coercion, labelled with native meter origin. Public facts contain closed
status values, stable credential-audited aliases, hashes and counts; raw bodies
and SDK objects are not exported. Native accepted IDs/request/scope/model facts
are separately retained in 0700 directories/0600 files for audit.

The [Interaction response schema](https://ai.google.dev/api/interactions-api)
marks `model` optional. For an agent response omitting it, accounting requires
the **independently captured accepted POST bytes/model/hash** and exact scoped
native identity; [agent model selection](https://ai.google.dev/gemini-api/docs/antigravity-agent#model-selection)
documents the request's `agent_config.model`. This basis is labelled
`accepted_post_bytes`, not a native model readback. Missing that provenance or
any conflicting native/agent-config/step model keeps actual unknown. Stale
contradictory counters taint completeness permanently; terminal counters must
cover every retained maximum and coherent output floor. Signed bound arithmetic
uses exact literals and precision-80 local contexts, independent of the caller.

Each accepted interaction receives **one final terminal GET before DELETE**.
The selected snapshot must match its accepted identity/model/session and be
fresh and complete. Decreasing, missing, stale, foreign or invalid counters
cannot replace max-seen facts or release a hold. Known overruns still latch the
shared guard and block the next reservation. Snapshots are cumulative and never
summed as new charges. Workflow PASS can retain `actual_usd=null` and a full hold;
financial uncertainty is always reported separately. Cleanup targets only the
accepted IDs; cleanup failure fails the case.

Each counter is captured independently at the owned HTTP response boundary,
before SDK decoding. A malformed sibling taints its own exactness and keeps
actual unknown; independently valid prompt, cached, candidate and thought counts
still contribute conservative floors. Public facts name only the tainted
categories, never publish their malformed values. A valid overrun floor latches
the shared guard even when another counter or the SDK decoding is invalid.
Category maxima are not an exact cache split: incomplete floor DTOs leave the
cache-write category unknown so minimum pricing uses the cheapest compatible
remainder, rather than treating an older cached zero as exact for a newer prompt.
Complete measured terminal DTOs still retain the reviewed no-cache-write zero.

Every admitted request seals its scope and accepted native target once. All
response bodies, including both cancel routes, readbacks and cleanup, and every
received raw SSE meter enter the same accumulator before SDK decoding. Stream
tapping is lazy and byte-preserving; partial frames and all same-chunk peers are
processed before codec refusal. SSE event IDs never identify interactions.
Explicit foreign interaction IDs are refused and cannot contribute charges.
The book retains maxima per interaction/category and a sticky interaction
overrun; settlement reads only that book. Only the retained final GET snapshot
certifies exact usage, and it must cover every observation, including later
cleanup meters. Cancel/stream bookkeeping cannot release host occupancy.

Any native root, agent-config or step model differing from `gemini-3.8-flash`
sets a sticky drift flag, retains unknown actual/full holds, refuses further
interaction creates and forces the case result to FAIL. Later primary-model
responses cannot clear that flag. Missing native model remains labelled request
provenance as described above; it is never replaced by an invented native model.

Offline tests use real SDK MockTransport request bytes, fake native responses,
disposable ledgers and no key access. Their numeric settlement is synthetic,
never canonical live spend or a new live PASS. After dual-sign/merge, N8 installs
and binds the line/envelopes and the lead pins GO for each case:

```text
python -m mux.testing.drivers.gemini.round_runner --live \
  --envelope <signed-case.json> --envelope-sha256 <lead-pin> \
  --go <separate-go.json> --go-sha256 <lead-pin> \
  --config <installed-round-budget.json> --output <fresh-pinned-absolute-path>
```

Only completed scoped scenario assertions produce PASS. C08/C09 remain driver
feature gaps. C01/C02/C05/C07/C12/C14/C17 lack complete host scenarios for this
round. F2 lacks real provider-readable resources/authenticated MCP and remains
unselected. No historical PASS case is repeated by this entry.
