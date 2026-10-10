# Cross-backend outcome oracle

`daimon.testing.outcome_oracle.evaluate(recording, assertions)` evaluates host
observations for Anthropic, OpenAI and Gemini without provider calls or a judge.
Slice 1 supports `turn_completed`, catalog `done_within_s`,
`no_session_replacement`, and `same_session {turn, previous_turn?}`. Remaining
catalog predicates currently return `PENDING / UNSUPPORTED_KIND` individually.

N9's runner constructs `RunEvidence` / `TurnEvidence` from actual host capture.
Use one monotonic clock for the trigger and terminal observations. Pin the
host-selected slot, session and root identity, and retain evidence IDs from the
capture. Terminal observations include their root/session and authority;
preview/gap, another root, another session, and session lifecycle termination
cannot certify completion. Conflicting authoritative outcomes fail. Completion
latency is the first matching authoritative outcome minus the trigger time; the
configured bound is inclusive. This is a root completion bound, not card-settle
or first-visible-output latency.

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

Offline tests: `uv run pytest -q packages/testing/tests/test_outcome_oracle.py`.
No keys, external API, staging database or paid judge are used.
