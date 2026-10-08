# Tool approval and Managed Agents pauses

`Approvals.tla` models one Managed Agents step with one to three gated calls. `Batched` selects one card for the step or one card per call. Either way the driver gathers all card answers, then sends **one batch** containing one `user.tool_confirmation` per call. This is already how single cards work: [`_decide_blocked`](../../packages/core/daimon/core/turn/driver.py#L112) gathers the per-call deciders and [`_consume_with_reconnect`](../../packages/core/daimon/core/turn/driver.py#L1155) sends their events together. Grouping cards changes only the click layer, not the MA request.

The model includes a first requester answer, expiry, cancellation, and up to two refused later or non-requester clicks. A pending card settles once. Cancellation sends deny for every pending call. MA can emit a duplicate `requires_action` while an ungated tool runs and after taking each queued confirmation. Echoed confirmation IDs enter `accepted`; a duplicate whose IDs are not all accepted is stale. `ReAsk` permits a genuine request for an already echoed ID. `DropConfirm` removes queued confirmations, then a quiet-stream status check ends the turn. `QuietStatusEarly` separately explores a status check while confirmations are still queued. `EarlyCardRender` exposes the card edit that precedes the driver send.

## Properties and bounds

- `AtMostOnce`: each call runs at most once.
- `NoFalseNotAccepted`: “not accepted” follows a genuine request for an echoed ID.
- `ApprovedRunsInTurn`: an allowed call does not run after the turn origin is removed.
- `NoAllowAfterStop`, `CardTruth`, `OneAnswerPerCard`: card decisions and sent events agree; a card settles once.
- `Terminates`: under weak fairness of `Next`, a turn reaches an end, including after a dropped confirmation. This fairness assumes enabled MA and timeout actions eventually occur. It is not a real-time bound.

The state space has one step, K at most three, one optional ungated tool, one duplicate pause, one possible genuine re-ask, and at most two refused clicks. It abstracts card posting, network failures, replay pagination, timeout duration, and the exact order of queued confirmations. MA observations in the brief (duplicate pauses, queued confirmations and echoes) come from live event logs rather than a documented MA delivery guarantee. In particular, the code cannot establish that an HTTP-accepted confirmation will be consumed before the driver’s next read timeout. The model treats a post-turn MA call as a run, while the MCP publish gate may refuse its effect because [`turn_origin`](../../packages/core/daimon/core/turn_origin.py#L152) deletes the origin on turn exit and [`require_publishable`](../../packages/adapters/mcp/daimon/adapters/mcp/tools/_channel_policy.py#L281) requires a verified origin.

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
| `ApprovalsQuietEarly` | 172 | violates `ApprovedRunsInTurn` |
| `ApprovalsEarlyCard` | 14 | violates `CardTruth` |

`ApprovalsPreFix`: MA pauses on two calls; both cards expire; the driver sends one deny batch; MA emits the same pause before echoing either confirmation. The old `fresh = {}` rule ends the turn as “not accepted” without a genuine re-ask. `ApprovalsPreFixLateRun` uses one expired card and one approved card: MA later takes the queued allow and runs that call after turn end. This is the #446 failure fixed by checking `accepted` in the [live loop](../../packages/core/daimon/core/turn/driver.py#L1193).

`ApprovalsQuietEarly` exposes a possible current-code gap. After a batch is sent, MA repeats `requires_action` while an allowed confirmation remains queued. A quiet stream triggers the [eventless status branch](../../packages/core/daimon/core/turn/driver.py#L724). It sees no fresh IDs, breaks and [finalizes](../../packages/core/daimon/core/turn/driver.py#L1244), removing the turn origin. MA then consumes its queued allow and runs the call. The live loop’s `accepted` check does not run on that branch. This trace depends on MA remaining idle long enough for the read timeout yet later taking a queued confirmation; we did not confirm whether MA permits that timing. If it does, today’s code is unsafe. The clean configs exclude this timing, so their verdict is conditional on that assumption.

`ApprovalsEarlyCard` is another current-code counterexample. The requester clicks Approve; [Discord settles the future and edits the card](../../packages/adapters/discord/daimon/adapters/discord/tool_confirmation.py#L100) to “Approved — running it.” The other card is still pending, so the [gather](../../packages/core/daimon/core/turn/driver.py#L123) has not completed and no allow has been sent. The card text makes a stronger promise than the driver has fulfilled. A batched card would also show an early approved state unless its edit waits until the batch send succeeds.
