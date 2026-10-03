# Turn bookkeeping extraction

`core/turn/bookkeeping.py` owns the orphan marker compare-and-clear, commit
before MA interruption, recovered card record/edit/retire sequence, and iteration
over a turn's touched mappings. Adapters inject their existing store and provider
calls. They retain snapshots, admission barriers, transport handling, retries and
transaction boundaries.

Discord and Slack use the shared orphan recovery and found-card reconciliation.
Teams settlement uses the shared mapping cleanup.
Discord and Slack render the shared `core/turn/card_state.py` reducer through their
existing card renderers. Teams keeps its existing card state.

Equivalence tests execute base functions from commit
`f324bb649a52500fe1c8928412394cb193850be4` against the same recorded rows,
boot states, failures and lookup results as the current adapter functions. They
compare store calls, commits, edits, MA interruptions, card output and sleeps.
The test fixtures retain the base executable statements and omit docstrings and
comments. The adapter tests also exercise the stores against Postgres.

Parked work:

- Teams boot recovery is touched by open PR #220.
- Discord mention, continuation and wizard bookkeeping are touched by #376.
- Slack mention and continuation bookkeeping are touched by #133, #145, #376 and #396.
- Teams turn entry bookkeeping is touched by #220 and #396.
- Recovery barriers and lookup miss scheduling stay with the adapters in this
  slice. Their timing and locking remain unchanged.
