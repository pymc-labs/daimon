# Formal state-machine coverage

This directory contains finite TLA+ abstractions of selected production state
machines. Source code and executable tests define the behavior; the models make
concurrency assumptions explicit and check the abstractions within stated
bounds. A passing TLC run does not prove the Python, database, adapter, or
upstream service correct.

## Coverage and priority

| Priority | State machine / concurrency boundary | Current formal coverage | Existing executable checks | Remaining boundary and rationale |
| --- | --- | --- | --- | --- |
| P0 | Turn event folding, reconnect replay, periodic/final rendering, cancellation | [`turn/`](turn/README.md) models reducer state and render anchor; separate bounded progress and replay configurations. | [`core/tests/turn/`](../packages/core/tests/turn/) covers reducers, render diff, driver, reconnect, cancellation, lifecycle hooks; [`tests/integration/test_discord_turn_e2e.py`](../tests/integration/test_discord_turn_e2e.py). | The model abstracts adapter effects, actual asyncio interleavings, eventless replay/status gating, and session recovery. See the turn README for checked counterexamples, assumptions, and bounds. This is the user-visible core path and first modeling priority. Source: [`driver.py`](../packages/core/daimon/core/turn/driver.py), [`reducers.py`](../packages/core/daimon/core/turn/reducers.py), [`render.py`](../packages/core/daimon/core/turn/render.py). |
| P0 | Routine scheduler lock, atomic claim, dispatch/result lifecycle | [`scheduler/`](scheduler/README.md) models two schedulers contending for one lock, a claimed occurrence, and bounded recurrence; safety plus a fairness-conditional progress configuration. | [`test_scheduler_tick.py`](../packages/core/tests/test_scheduler_tick.py), [`test_advisory_lock.py`](../packages/adapters/scheduler/tests/test_advisory_lock.py), [`test_routines_end_to_end.py`](../tests/integration/test_routines_end_to_end.py). | SQL claim/result operations are atomic model steps. Process death, ambiguous commit, and external side effects after claim are excluded; see scheduler README. |
| P1 | Adapter turn surface, cancel registration, dead-session recovery, durable active-turn marker and boot cleanup | Not modeled. | [`discord/tests/test_lifecycle.py`](../packages/adapters/discord/tests/test_lifecycle.py), [`discord/tests/test_orphaned_turns.py`](../packages/adapters/discord/tests/test_orphaned_turns.py), [`slack/tests/test_lifecycle.py`](../packages/adapters/slack/tests/test_lifecycle.py), [`slack/tests/test_orphaned_turns.py`](../packages/adapters/slack/tests/test_orphaned_turns.py); integration turn tests above. | Recovery swaps sessions/lifecycle objects while adopting the already-posted surface; cancel ownership and durable marker cleanup span awaits, database writes, and platform calls. Model the shared transition contract across adapters, then check parity-specific details. Sources: [`Discord bot`](../packages/adapters/discord/daimon/adapters/discord/bot.py), [`Slack app`](../packages/adapters/slack/daimon/adapters/slack/app.py), [`Slack boot sweep`](../packages/adapters/slack/daimon/adapters/slack/boot_sweep.py). |
| P1 | Per-thread session preparation and successor creation | Not modeled. | [`test_session_preparation.py`](../packages/core/tests/test_session_preparation.py), [`test_session_preparations.py`](../packages/core/tests/stores/test_session_preparations.py), turn admission and preparation tests. | A tuple-scoped PostgreSQL advisory lock serializes compatibility decisions and replacement; model retries, partial replacement stages, active-turn deferral, and failure recovery. Source: [`session_preparation.py`](../packages/core/daimon/core/session_preparation.py), [`session_preparation_stages.py`](../packages/core/daimon/core/session_preparation_stages.py). |
| P1 | Task handoff continuation: pending → claimed → delivered/skipped | Not modeled. | [`test_task_continuations.py`](../packages/core/tests/stores/test_task_continuations.py), [`continuity/test_continuation.py`](../packages/core/tests/continuity/test_continuation.py), Discord/Slack continuation dispatch tests. | Conditional database update is the cross-process at-most-once claim. The dispatch and settlement boundary can leave a durable claimed row if a process dies; model delivery ambiguity and terminal settlement. Source: [`task_continuations.py`](../packages/core/daimon/core/stores/task_continuations.py), [`continuation_dispatch.py`](../packages/adapters/discord/daimon/adapters/discord/continuation_dispatch.py). |
| P2 | Git repository skill resynchronization and retry/backoff | Not modeled. | [`test_resync.py`](../packages/core/tests/skill_sync/test_resync.py). | Bounded transient retries and persisted sync errors matter for convergence, but are less central than turn/session dispatch. Model when broadening coverage. Source: [`resync.py`](../packages/core/daimon/core/skill_sync/resync.py). |

## Explicit exclusions

The inventory above prioritizes stateful workflows with concurrency, retries,
or externally visible terminal effects. It does not model pure decision
functions or view builders; ordinary CRUD without a multi-step lifecycle;
individual OAuth/billing flows; tenant onboarding; MA agent/environment
reconciliation and version-conflict retries; credential and vault updates;
notebook/report hosting; or platform SDK internals. These remain covered by
their unit, integration, contract, and database tests where present. Revisit
these exclusions if a concrete race or reliability requirement makes them
consequential.

## Running the current models

See [`turn/README.md`](turn/README.md) and
[`scheduler/README.md`](scheduler/README.md) for exact sequential TLC commands,
configuration names, bounds, fairness assumptions, checked properties, and
counterexample interpretation. Keep TLC runs sequential in one checkout when
they share a state directory. Add source-linked variable/action mappings and
environment assumptions to each model's README as coverage grows.
