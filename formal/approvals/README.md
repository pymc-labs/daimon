# Tool approval and Managed Agents pauses

`Approvals.tla` models one Managed Agents step with one to three gated calls. `Batched` selects one card for the step or one card per call. Either way the driver gathers all card answers, then sends **one batch** containing one `user.tool_confirmation` per call. This is already how single cards work: [`_decide_blocked`](../../packages/core/daimon/core/turn/driver.py#L112) gathers the per-call deciders and [`_consume_with_reconnect`](../../packages/core/daimon/core/turn/driver.py#L1155) sends their events together. Grouping cards changes only the click layer, not the MA request.

The model includes a first requester answer, expiry, cancellation, and up to two refused later or non-requester clicks. A pending card settles once. Cancellation sends deny for every pending call. MA can emit a duplicate `requires_action` while an ungated tool runs and after taking each queued confirmation. Echoed confirmation IDs enter `accepted`; a duplicate whose IDs are not all accepted is stale. `ReAsk` permits a genuine request for an already echoed ID. `DropConfirm` removes queued confirmations, then a quiet-stream status check ends the turn. `QuietStatusEarly` explores a status check while confirmations are still queued. `QuietRetryBound` counts extra idle cycles. `QueueSettlesWithinBound` assumes MA consumes or drops a queued confirmation by the second cycle. `EarlyCardRender` exposes the card edit that precedes the driver send. `RetireUnsentApproved` models the refusal path retiring an answered Approve card to Stopped when its allow was never sent.

**Batching verdict:** One card for several calls is as safe as one card per call under gather-then-send: both produce the same MA confirmation batch, and the checked safety properties hold in both configurations. Batching stays off by Derick's decision on 2026-10-08 until production runs clean on single cards.

## Properties and bounds

- `AtMostOnce`: each call runs at most once.
- `NoFalseNotAccepted`: “not accepted” follows a genuine request for an echoed ID.
- `ApprovedRunsInTurn`: an allowed call does not run after the turn origin is removed.
- `NoAllowAfterStop`, `CardTruth`, `OneAnswerPerCard`: card decisions and sent events agree; a card settles once. Early rendering can temporarily precede the send, so the fixed card configuration checks `EventualCardTruth`: after turn end, final card states agree with what was sent.
- `Terminates`: under weak fairness of `Next`, a turn reaches an end, including after a dropped confirmation. This fairness assumes enabled MA and timeout actions eventually occur. It is not a real-time bound.

The state space has one step, K at most three, one optional ungated tool, one duplicate pause, one possible genuine re-ask, and at most two refused clicks. It abstracts card posting, network failures, replay pagination, timeout duration, exact event-ID redelivery, and the exact order of queued confirmations. Driver tests exercise redelivery of the same pause event ID after replayed confirmation echoes. MA observations in the brief (duplicate pauses, queued confirmations and echoes) come from live event logs rather than a documented MA delivery guarantee. In particular, the code cannot establish that an HTTP-accepted confirmation will be consumed before the driver’s next read timeout. The model treats a post-turn MA call as a run, while the MCP publish gate may refuse its effect because [`turn_origin`](../../packages/core/daimon/core/turn_origin.py#L152) deletes the origin on turn exit and [`require_publishable`](../../packages/adapters/mcp/daimon/adapters/mcp/tools/_channel_policy.py#L281) requires a verified origin.

## TLC evidence

Run `TLA2TOOLS_JAR=/path/to/tla2tools.jar formal/check.sh`. CI reads every row from [`expected.tsv`](../expected.tsv), so these configs enter the existing Formal models job without a workflow edit. TLC 2.19, one worker:

| Config | Distinct states | Verdict |
| --- | ---: | --- |
| `ApprovalsSingle` (K=3) | 5,489 | clean |
| `ApprovalsBatched` (K=3) | 593 | clean |
| `ApprovalsProgress` | 935 | clean, `Terminates` |
| `ApprovalsPreFix` | 162 | violates `NoFalseNotAccepted` |
| `ApprovalsPreFixLateRun` | 346 | violates `ApprovedRunsInTurn` |
| `ApprovalsReAsk` | 413 | clean; a genuine re-ask ends “not accepted” |
| `ApprovalsDroppedConfirm` | 629 | clean, `Terminates` through status timeout |
| `ApprovalsQuietEarly` | 143 | violates `ApprovedRunsInTurn` |
| `ApprovalsQuietBounded` | 836 | clean, `Terminates` |
| `ApprovalsQuietLateBeyondBound` | 420 | violates `ApprovedRunsInTurn` |
| `ApprovalsEarlyCard` | 1,034 | clean, `EventualCardTruth` |
| `ApprovalsEarlyCardOld` | 1,061 | violates `EventualCardTruth` |
| `ApprovalsReplayQueued` | 1,566 | violates `NoFalseNotAccepted` |

`ApprovalsPreFix`: MA pauses on two calls; both cards expire; the driver sends one deny batch; MA emits the same pause before echoing either confirmation. The old `fresh = {}` rule ends the turn as “not accepted” without a genuine re-ask. `ApprovalsPreFixLateRun` uses one expired card and one approved card: MA later takes the queued allow and runs that call after turn end. This is the #446 failure fixed by checking `accepted` in the [live loop](../../packages/core/daimon/core/turn/driver.py#L1193).

`ApprovalsQuietEarly` retains the old eventless exit: the driver sends an allow, MA repeats `requires_action` while the confirmation remains queued, and the driver ends the turn. MA then takes the allow and runs the call after the origin is deleted.

`ApprovalsQuietBounded` models the new two-cycle reconnect rule. The driver never resends the allow. MA either echoes and completes the turn or drops the still-queued confirmation by the bound; the latter ends as `couldnt_confirm`. All safety invariants and weak-fair termination hold in 836 states. `ApprovalsQuietLateBeyondBound` keeps MA allowed to consume its queue after the bound. It still violates `ApprovedRunsInTurn`: two retries delay the failure but cannot revoke a queued allow. The code's bound is therefore a mitigation, not an unconditional safety proof. The assumption that MA settles the queue within two cycles has not been confirmed by an upstream guarantee. See the [eventless branch](../../packages/core/daimon/core/turn/driver.py#L724) and [turn origin lifetime](../../packages/core/daimon/core/turn_origin.py#L194).

`ApprovalsEarlyCardOld` is the old refusal-path counterexample. One card shows Approved before gather-then-send finishes; cancellation sends deny for the pending calls but leaves that card Approved after the turn ends. With `RetireUnsentApproved`, the refused, unsent approval becomes Stopped, and `ApprovalsEarlyCard` satisfies eventual card truth. An approved card can still appear before the allow is sent; the property concerns the final card state. The model treats retirement as completed on refusal. The implementation uses bounded best-effort callbacks, so a failed card edit remains an operational limitation rather than a proved guarantee.

`ApprovalsReplayQueued` shows a counterexample found on staging on 2026-10-09 (stress run 4). The event history returns a `user.tool_confirmation` as soon as MA has queued it, before MA has taken it. MA also takes a batch one confirmation per pause. The old driver counted a confirmation in a replay as taken. Six expired cards sent six denies, and the stream closed before MA took any of them. The replay listed all six, and the next one-per-pause idle (five calls left) ended the turn as "not accepted". The model separates `known` (what the driver believes MA took) from `accepted` (what MA took). `ReplayQueued` lets a replay add queued confirmations to `known`. The driver now counts a call as taken only once its tool result appears, which `Take` models. So every current-code config sets `ReplayCountsQueued = FALSE` and keeps its earlier state count.
