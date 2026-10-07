# Thread queue

`core/turn/thread_queue.py` owns author grouping, queued batch draining,
out-of-turn dispatch admission, dispatch/drain ordering and deferred resumes.
The adapters supply message composition, replies, turn execution and caps.
Its views use the existing collections, so callers and shutdown drains observe
the same state. Busy arrivals append before the wait response.

Discord drains after a dispatch exception and keeps the first carrier message.
Slack drains after success, skips authorless events, isolates expected errors
per author and apologises to leftover mentions. It removes the ⌛ from each
queued message when its batch settles, and from messages taken from the queue
but not run, or left in it, when the drain or owning turn exits. Teams keeps the last carrier,
routes each drained turn at execution time, and caps wakes only. Its deferred
requests retain saved-input URLs and combine the capped flags with AND.
Cancellation retains each adapter's existing cleanup.

Process and tenant caps, recovery barriers, session fences, locks, authorization,
pins, seals and send rechecks stay in their existing functions. Further
extraction of the in-turn tail from Discord `_orchestrate_observed` and Slack
`_run_thread_turn_observed`, and the Slack admission front door, is parked
because open teammate PRs edit those functions. They remain turn callbacks.

The equivalence tests execute frozen base methods from
`14eaeb1bbcadf10b97beb0cedcf838e394fa3ee3` and the current callers with identical
arrivals, completions, errors and cancels. They compare admission order,
responses, coalesced messages/files, continuation writes and final queue state.

The merge matrix uses real `git merge --no-commit --no-ff` in both orders in a
disposable checkout inside this worktree. Every trial is aborted. Compared with
main, each overlapping PR has the same conflict files and conflict hunk counts.

| PR | Pre-existing conflict files, either order | New conflicts, either order |
| --- | ---: | ---: |
| #396 | 8 | 0 |
| #389 | 2 | 0 |
| #376 | 0 | 0 |
| #220 | 28 | 0 |
| #145 | 18 | 0 |
| #133 | 9 | 0 |
