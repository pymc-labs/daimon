# Slack event dedupe, redelivery and drain

Run from the repository root with a JRE and the pinned TLA+ tools jar described
in the [coverage report](../README.md). `formal/check.sh` runs every
configuration below against `formal/expected.tsv`. To run one configuration by
hand:

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to tla2tools.jar}"
cd formal/slack_dedupe
java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -config SlackDedupe.cfg SlackDedupe.tla
```

`SlackDedupe.tla` models Slack `app_mention` admission for one thread. There are
three distinct user messages: A, then B, then A again. Steps:

1. Slack delivers each message once (`Deliver`). It can redeliver the same
   logical event with a fresh envelope id (`Redeliver`: a lost ack, a
   reconnect, or another Socket Mode connection), but only within `Window`
   ticks of the first delivery.
2. `on_request` acks before any work and spawns one handler task per delivery.
3. The handler checks the draining flag (`CheckDraining`).
4. The handler inserts and commits the `(team_id, channel, event_ts)` dedupe row
   (`Dedupe`). On a conflict the delivery is dropped.
5. The handler either takes the thread or queues behind the running turn
   (`Orchestrate`).

The running turn ends in success or failure (`FirstTurnEnds`). The drain loop
pops the queue (`PopQueue`), partitions it by author, and runs one turn per
author (`DrainTurn`). `Release` is the `finally` block. The scheduler prunes
dedupe rows older than `Retention` ticks (`Prune`). The process can crash, or
finish a SIGTERM drain (`StartDrain`, `Exit`). Either way it loses in-memory
state and a replacement takes over (`Restart`). Acked events are not
redelivered, and committed dedupe rows survive.

| Model item | Implementation |
| --- | --- |
| ack-first, handler spawn | `on_request` in [`app.py`](../../packages/adapters/slack/daimon/adapters/slack/app.py) (`send_socket_mode_response` before dispatch) |
| `CheckDraining`, `Dedupe` | `_handle_app_mention` in [`app.py`](../../packages/adapters/slack/daimon/adapters/slack/app.py); [`slack_event_dedup.insert_if_new`](../../packages/core/daimon/core/stores/slack_event_dedup.py); PK in migration [`0001_initial_schema.py`](../../packages/core/alembic/versions/0001_initial_schema.py) |
| `Orchestrate`, `busy`, `pending` | `_orchestrate` in [`app.py`](../../packages/adapters/slack/daimon/adapters/slack/app.py) (`_processing`, `_pending`) |
| `FirstTurnEnds`, `PopQueue`, `DrainTurn`, `Release` | `_orchestrate` turn, drain loop (`_author_id` partition, per-author `try`), `finally` notice; `_handle_app_mention` error boundary (`render_error`) |
| `Prune`, `Retention` | [`slack_event_dedup_sweep.py`](../../packages/core/daimon/core/slack_event_dedup_sweep.py) (`_RETENTION = 7 days`), [`delete_event_dedup_older_than`](../../packages/core/daimon/core/stores/slack_event_dedup.py) |
| `StartDrain`, `Exit` | `drain_and_close` in [`app.py`](../../packages/adapters/slack/daimon/adapters/slack/app.py) waits for tracked app-mention handlers as well as `_processing`, [`__main__.py`](../../packages/adapters/slack/daimon/adapters/slack/__main__.py) |

Invariants:

- `AtMostOneTurn`: each delivered mention appears in at most one turn.
- `TurnPrincipal`: one turn runs only one author's mentions.
- `NoSilentLoss`: once the adapter is idle, every delivered mention got a turn
  or an error notice.
- `NoUndocumentedLoss`: the same as `NoSilentLoss`, but it exempts a mention the
  draining check rejected. That is the documented IN-02 drop.

| Config | Toggles vs `SlackDedupe.cfg` | Verdict | Distinct states |
| --- | --- | --- | --- |
| `SlackDedupe` | `Window = 2`, `Retention = 3`, no exits | clean (`AtMostOneTurn`, `TurnPrincipal`, `NoSilentLoss`) | 953,059 (depth 27) |
| `SlackPre997b3d8` | `PartitionByAuthor = FALSE` | violates `TurnPrincipal` | 268,977, trace 16 |
| `SlackPre0cda77e` | `NotifyOnFailure = FALSE` | violates `NoSilentLoss` | 595, trace 6 |
| `SlackShortRetention` | `Retention = 1 < Window` | violates `AtMostOneTurn` | 850,886, trace 18 |
| `SlackExitSafety` | `AllowExit`, `AllowDrain`, `MaxClock = 1`; checks `AtMostOneTurn`, `TurnPrincipal` | clean | 728,529 (depth 27) |
| `SlackCrashLoss` | `AllowExit` (plain crash, any time after the ack) | violates `NoSilentLoss` | 48, trace 4 |
| `SlackDrainWindow` | `AllowExit`, `AllowDrain`; checks `NoUndocumentedLoss` | clean | 3,877,947 (depth 31) |

## Calibration

| Bug | Pre-fix counterexample | Post-fix (`SlackDedupe`) |
| --- | --- | --- |
| 997b3d8 (#139): the drain loop composed every queued mention into one turn run as `queued[0]`, so B's instructions ran in A's session, under A's vault, and billed to A | `TurnPrincipal`, 16 states: A's turn runs, and B then A queue behind it. The pop merges `{B, A}` under B's author | clean, 953,059 states |
| 0cda77e (#118): a failed mention turn logged and returned, leaving the thread silent | `NoSilentLoss`, 6 states: A is delivered, deduped and takes the thread, and its turn fails with nothing posted | clean |

## Findings

The audit found and closed one graceful-drain loss window. What the model
establishes:

- **At most one turn per delivered mention holds.** It holds across
  redeliveries, a second connection racing the first, crashes, and SIGTERM
  drains (`SlackDedupe`, `SlackExitSafety`). The reason is that the dedupe row
  is committed before the turn and outlives the process. It depends on the
  prune's retention exceeding Slack's redelivery window.
  `SlackShortRetention` shows the failure when it does not. The real margin is
  7 days against about 6 minutes of retries, or 24 hours if "Delayed Events"
  were enabled, which it is not. The model also assumes a handler reaches
  `insert_if_new` within one tick of its delivery (`Tick` is disabled while any
  handler is before the insert). Without that assumption, a handler parked for
  longer than the retention could insert after the prune. In the code the
  longest wait before the insert is the boot orphan-recovery gate (seconds to
  minutes).
- **Documented residual (at-most-once): a crash at any point after the ack
  loses the mention** (`SlackCrashLoss`), whether it lands before or after the
  dedupe commit. The shortest trace is a delivery followed by an exit while the
  handler is still spawned, before its dedupe insert. Slack got its ack, so it
  does not redeliver. After the commit, a redelivery would be dropped by the
  committed row anyway. The same holds
  for queued mentions and a drain whose 50 s grace expires.
- **Graceful drain now waits for acked mention handlers as well as
  `_processing`** (`SlackDrainWindow`). This closes the pre-orchestration window
  when a handler is waiting on orphan recovery, dedupe, token lookup, or setup
  binding. The wait shares the existing 50-second grace bound; a handler still
  active when that bound expires can still be lost. The model's successful
  drain path assumes the grace window has not expired.
- **Hard-crash residual: process death after the ack can lose a mention**
  (`SlackCrashLoss`), before or after the dedupe commit. Slack retries failed,
  unacked Socket Mode events, but an acked event is considered received.
  Retrying a durable job after a crash is not generally safe: the process can
  die after the Managed Agents turn has incurred usage or performed a tool or
  external action but before the job is marked complete, so replay could double
  bill or repeat effects. This needs an explicit at-most-once versus
  at-least-once product decision plus idempotency boundaries before runtime
  replay is added.
- The uninstall teardown (`delete_event_dedup_for_team`) also removes rows. A
  redelivery of a pre-uninstall event after a reinstall within Slack's retry
  window would be admitted again. This is not modelled, because it needs an
  uninstall and a reinstall within about 6 minutes.

## Bounds and assumptions

- One thread and three mentions from two authors. `MaxRedeliveries = 1`, and
  `MaxClock = 3` (1 for `SlackExitSafety`). There is one adapter process at a
  time. A replacement is a restart with empty memory. The graceful-drain model
  covers completion within the grace period; expiry is represented by the
  hard-exit residual.
- A second Socket Mode connection receiving a redelivery is modelled as a
  concurrent handler. The dedupe PK serializes the handlers. Per-thread
  serialization (`_processing`) is in-memory and per process. Two replicas
  receiving different mentions for one thread would each run a turn. The model
  does not cover that, and it should not be scaled out without a shared lock.
- Turns succeed or fail atomically. A failed drained turn is notified
  (per-author isolation after 997b3d8). A first-turn failure notifies its
  mention (after 0cda77e) and every queued mention (`finally`).
- Not modelled:
  - the Slack Connect rejection and missing-token drop after the dedupe
    commit (both post or log explicitly);
  - bot echo filtering;
  - the per-tenant concurrency shed (an ephemeral notice);
  - authorless events;
  - the ⌛ reaction ordering (WR-05, already fixed before v0.1.0).
- TLC checks this finite abstraction only. The executable coverage is
  [`test_slack_event_dedup.py`](../../packages/core/tests/test_slack_event_dedup.py),
  [`test_slack_event_dedup_sweep.py`](../../packages/core/tests/test_slack_event_dedup_sweep.py)
  and the Slack adapter's drain and partition tests in
  [`test_app.py`](../../packages/adapters/slack/tests/test_app.py).
