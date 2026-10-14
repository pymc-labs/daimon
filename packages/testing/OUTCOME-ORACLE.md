# Cross-backend outcome oracle

`daimon.testing.outcome_oracle.evaluate(recording, assertions)` evaluates host
observations for Anthropic, OpenAI and Gemini without provider calls or a judge.
`SUPPORTED_ASSERTION_KINDS` lists the supported predicates: root completion,
terminal and first-visible latency, session reuse, visible text, progress cards,
tool/approval effects and host log presence/absence. Remaining catalog predicates
return `PENDING / UNSUPPORTED_KIND` individually. See the coverage matrix in the
READY report; this module does not certify a complete TARGET-53 catalog run.

N9's runner constructs `RunEvidence` / `TurnEvidence` from actual host capture.
Use one monotonic clock for the trigger and terminal observations. Pin the
host-selected slot, session and root identity, and retain evidence IDs from the
capture. Terminal observations include their root/session and authority;
preview/gap, another root, another session, and session lifecycle termination
cannot certify completion. Conflicting authoritative outcomes fail.
`turn_completed` requires a successful root outcome; catalog `done_within_s`
accepts any authoritative terminal outcome (including expected stop/error).
The inclusive deadline compares terminal observation time to trigger time plus
the bound, including fractional clock origins. Card-settle and first-visible
latency are separate checks.

Capture **every** binding selection/replacement through the settled turn window
as a `SessionUse`, including transient replacements that later revert. Set
`session_capture_complete` only after that window closes. Session reuse checks
require the same slot and session across both turns and all observed uses. A
single final snapshot without replacement instrumentation is incomplete capture.
Likewise, set `terminal_capture_complete` only after closing the root's capture
window. Explicitly missing coverage is `PENDING`; a complete capture missing the
required positive evidence is `FAIL`. Invalid evidence raises validation errors
and must never be converted into a passing report by the runner.

Every `CheckResult` carries its kind, turn, status, stable reason code and source
evidence IDs. `OutcomeReport.status` is FAIL if any check fails, otherwise PENDING
if any check is pending or the assertion list is empty, otherwise PASS.
`normalized_outcomes` omits native IDs, exact timestamps and wording and can be
compared across backends running the same ordered scenario assertions. Each
backend must independently PASS; equal FAIL/PENDING signatures are not parity
success. This does not replace the immutable Anthropic byte-level goldens.

For the effects slice, capture actual host effects as `VisibleText`, `CardUpdate`,
`ToolEffect`, `ApprovalEffect`, `ReactionEffect` and `LogObservation`. Every effect
has an evidence ID, monotonic observation time and globally unique `order` within
the turn. That order resolves equal-clock approval/execution races; each domain's
stream must retain capture order. Set domain coverage flags only after the
settled observation window closes. Preserve transient visible states and every
progress card, not only the final answer.

- `text_present` / `text_absent {turn, pattern}` apply the supplied regex to all
  visible content, including embed titles/descriptions/fields/footers, and joined
  chunks. The runner must flatten those visible leaves into `VisibleText`; agent
  thoughts or hidden provider messages are not visible evidence.
  Joining includes both an empty separator and a newline: a pattern can span
  separate messages. Presence waits for complete text capture; observed forbidden
  text fails immediately. This conservative presence rule differs from the
  sufficient positive observations used for host logs and first-visible latency.
- `no_preamble {turn, pattern?}` requires a nonblank visible answer and rejects
  the supplied pattern or the documented default: "came across", conversation
  moved/continued, or "working files could not be saved". It is a deterministic
  ban on those phrases, not an unrestricted semantic/LLM judgment. An earlier
  visible preamble cannot be erased by editing the final message.
- `card_finalized {turn}` requires every card's latest state to be finalized.
  `progress_seen {turn, within_s?}` requires observed progress, optionally before
  the inclusive deadline. Root completion alone does not prove card finalization.
  Zero captured cards fails even with complete capture; this is stricter than
  the catalog's absence-of-progress definition. Attach this stricter predicate
  to fixtures expected to produce cards; card-less catalog fixtures need an
  explicit semantic binding rather than inferring that a card was finalized.
- `tool_succeeded {turn, tool_name, min?, max?}` counts distinct successfully
  executed calls. Each call has exactly one actual executor start and one result
  with the same call ID/name, in order; malformed extra calls fail. Tool names are
  logical host tool names, preserved exactly. A model's claimed action is not an
  executed effect.
- `approval_effect {turn, tool_name, decision, min?, max?}` requires linked request
  and decision observations. Approved calls must execute successfully after the
  decision; denied/timed-out calls must never execute. Multiple approval cards
  cannot multiply the number of calls. Capture human/policy decisions at the host
  approval boundary, not from text saying "approved".
  Every captured execution of the gated tool needs its own approved call ID;
  an approved/denied action cannot authorize an additional unlinked call.
- `reply_within_s` / `no_silent_drop {turn, max}` require an actual visible text,
  card or reaction before the inclusive deadline. Queue/error notices count for
  these visibility checks and do not imply root success. A positive observed
  response proves visibility even if another visible domain was not captured;
  proving silence requires all three domains to be captured completely.
- `log_present` / `log_absent {turn, event, fields?}` match host event names and
  exact supplied field values. This supports the catalog's replacement reason
  probes without guessing log absence from unchanged final session IDs.
  List-valued fields match exactly, including order; `["skills", "model"]` does
  not satisfy `["skills"]`. N9 preserves that assertion or supplies the actual
  expected list rather than silently using containment. Catalog turn 0 denotes
  setup/admin logs and returns `PENDING / TURN_NOT_CAPTURED` until setup capture
  is bound; it is a valid assertion, not malformed input.

Replay without a provider: serialize `RunEvidence.model_dump_json()`, reload with
`RunEvidence.model_validate_json()`, then call `evaluate` with the same resolved
assertion dictionaries. N9 resolves catalog placeholders before evaluation.
Reports contain reason codes and evidence IDs, not captured message/log values.
No CLI/HTTP/SQL/pytest command in a catalog assertion is executed by this oracle.

Offline tests: `uv run pytest -q packages/testing/tests/test_outcome_oracle.py`.
No keys, external API, staging database or paid judge are used.
