# Teams activity claim

Question: can the same Bot Framework activity run a second turn after a worker crash and redelivery?

| Action | Code boundary |
| --- | --- |
| Deliver, DropDuplicate | `handle_message` in `packages/adapters/teams/daimon/adapters/teams/app.py` |
| Claim | `handle_message` transaction and `stores/teams_activity_claims.py:claim` |
| Run, Finish | `_run_turn` and its outcome-linked claim update |
| Crash | `drain` cancellation or worker death; the committed row survives |

One activity, at most two deliveries, and at most one crash are the finite bounds. The claim is atomic at the database commit before `spawn`. `Durable = FALSE` models the previous in-memory `_seen`, which is lost on crash. `NoSecondRun` is the safety property. The safe case suppresses redelivery even when the first run did not finish; the existing boot recovery card explains the interruption if a card was created. A crash between claim and card creation can leave an activity with no visible notice. There is no automatic replay because external effects may already have happened.

The model does not include Bot Framework retry timing, turn queue batching, or claim pruning. It assumes any redelivery occurs within the 30-day retention window; this service contract has not been verified. The unsafe counterexample is replayed by `packages/adapters/teams/tests/test_activity_claim.py` against the pre-fix handler and passes after the durable claim is restored.
