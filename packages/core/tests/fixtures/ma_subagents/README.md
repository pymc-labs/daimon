Recorded 2026-10-31 against the staging Managed Agents API (spike in the PR description):
a Sonnet 5.5 lead with a `coordinator` roster of one Haiku 5.5 helper.

- `fanout.jsonl`: the primary stream of one turn that delegated to two helpers in parallel.
- `threads.json`: `GET /v1/sessions/{id}/threads` after that turn.
- `thread_events_<suffix>.json`: `GET /v1/sessions/{id}/threads/{thread}/events` for each helper.
- `interrupt.jsonl`: the primary stream of a fan-out stopped by one session-wide `user.interrupt`.
- `budget_reached.jsonl`: the primary stream of a fan-out that hit a 1-cent session budget.
