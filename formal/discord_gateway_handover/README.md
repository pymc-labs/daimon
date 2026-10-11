# Discord gateway handover during a deploy

Question: while two Discord adapter processes overlap in a deploy (old draining, new standby), does a PostgreSQL advisory lock (the "gateway lease") on each process's owner connection guarantee (a) at most one process is connected to the Discord gateway at a time, and (b) on a graceful deploy the time with no connected process is bounded by the new process's connect step, with no gap that includes process start, imports or runtime build?

Status: the lease protocol is **designed, not implemented**. Steps marked "designed" have no code to cite; everything else cites the code on this branch. Checked within these bounds, not proved for production numbers. Provisional: no counterexample has been replayed against real code and no trace has been validated.

## Result in short

- (a) holds for the designed protocol, including one owner-connection loss, one SIGKILL and a third process, provided the heartbeat grace outlasts the holder's fail-stop (assumption A5) and the new holder clears `released_at` when it takes the lease.
- (b) does **not** hold for the deploy as literally designed ("start new replica alongside old, then `docker stop` old"). If SIGTERM reaches Old before New has built its runtime, an idle Old closes the gateway within about a second and the gap includes New's start-up. It holds only if the deploy waits for New to be ready before stopping Old (`GatewayNoWaitReady` vs `GatewaySafe`).
- Today's stop-first recreate (`deploy.yml:372`) has the full gap (`GatewayStopFirst`).
- "Bounded by the connect step" is bounded by connect plus New's lock-poll interval, which the model does not measure (see Not modelled).

## Model

Process 1 (Old) starts connected, holding the lock, with the lease row naming it. Process 2 (New) is started by the deploy. Process 3 exists only in the three-process configs (a later restart). Each action is one `await`/commit boundary.

| Model action | Code source |
| --- | --- |
| `StartNew`, `Build` | `packages/adapters/discord/daimon/adapters/discord/__main__.py:57` (`build_runtime`), `runtime.py:216-262` (engine, owner connection, `hold_worker_owner`). Designed: process start order is set by the deploy. |
| `PollLock` | Designed: `pg_try_advisory_lock(GATEWAY_KEY)` on the owner connection. Existing analogue for the owner key: `runtime.py:68-73`. |
| `ReadLease`, `Recheck`, `Take` | Designed: lease row read, `owner_is_alive(prev)` wait (`packages/core/daimon/core/stores/worker_ownership.py:23-48`), holder write with `released_at` cleared. |
| `Connect` | `__main__.py:78` (`bot.start`). Designed: gated on the lease. Today it runs right after `build_runtime`, with no gate. |
| `Term` | `__main__.py:65` (SIGTERM handler -> `_drain_and_close`); `.github/workflows/deploy.yml:367-372` (`up -d --timeout 95`). Designed: start New alongside, then `docker stop` Old. |
| `DrainDone` | `bot.py:800-820` (`draining=True` at `bot.py:807`, wait for in-flight up to `_DRAIN_GRACE_S`, `bot.py:233`). |
| `CloseGw` | `bot.py:850` (`await self.close()` at the end of `_drain_and_close`). |
| `WriteReleased`, `Unlock` | Designed: set `released_at`, then unlock the gateway lock. Owner-lock analogue: `runtime.py:178-187`. |
| `Exit` | `runtime.py:171-193` (teardown deletes the heartbeat row and unlocks when the connection is alive, else `discard()`). |
| `Kill` | `deploy.yml:367-372` (95 s `docker stop` timeout, then SIGKILL; `stop_grace_period` 90 s per the deploy comment). |
| `ConnLoss` | A PostgreSQL backend dies; the lock disappears at once (assumption A3). Detected by `runtime.py:130-143`. |
| `Probe`, `Recover`, `GiveUp` | `runtime.py:133-167` (probe every 5 s, retry for `OWNER_RETRY_BUDGET_S`, reacquire needs an unexpired heartbeat, `ownership_lost.set()` on failure). Designed: recovery also needs the gateway lock back, otherwise the lease is lost. |
| `HardExit` | `__main__.py:29-38` (`drain_on_owner_loss`: drain, then `os._exit(1)`, with a `call_later(OWNER_HARD_EXIT_S, os._exit)` backstop). |
| `GraceElapsed` | `worker_ownership.py:23-48` (database-clock heartbeat age against `OWNER_LOSS_GRACE_S`). |

State: `phase`, `gw` (connected to Discord), `draining`, `termed`, `drained`, `conn` (owner connection alive, which holds both advisory locks), `graceOpen` (heartbeat row unexpired), `ownerState` (the process's belief: ok/retry/lost), `lockHolder`, lease row (`leaseHolder`, `leaseRel`), `claim` (none/held/wait/go/taken), `relStage`, and history flags `everConn`, `lossSeen`, `killSeen`.

### Properties

| Property | Meaning |
| --- | --- |
| `AtMostOneHandler` | Never two processes connected to the gateway at once. This is strict: a draining process still receives events and edits cards. |
| `AtMostOneAccepting` | Weaker: never two processes connected and not draining. A draining process rejects new mentions (`bot.py:1930,2012`). |
| `GapBounded` | In a run with no connection loss and no SIGKILL, every state where nobody is connected, Old has closed and New has not yet connected has New already past start-up (`phase = ready`: runtime built, polling). |
| `NewEventuallyConnects` | Liveness, weak fairness on every protocol step and on the deploy's SIGTERM to Old (not on loss or kill, which are bounded to one each). Checked with `GatewayProgress`. |
| Witnesses | `NoUncleanHandover`, `NoKillHandover`, `NoRecovery`, `NoWaiting` are meant to be violated: they show those paths are reachable. |

### Constants and scaling

| Constant | Value | Source |
| --- | --- | --- |
| Processes | 2 (Old, New); 3 in `GatewayThree`, `GatewayStaleRelease` | Minimum for a handover; the third shows a stale lease row. Production has no process cap. |
| Connection loss, SIGKILL | At most one of each per run | Enough to show the unclean path; more adds states, not new orderings. |
| `OWNER_PROBE_INTERVAL_S` 5, `OWNER_PROBE_TIMEOUT_S` 5, `OWNER_RETRY_BUDGET_S` 30, `OWNER_HARD_EXIT_S` 100, `OWNER_SCHEDULING_MARGIN_S` 40, `OWNER_LOSS_GRACE_S` 180 | Logical order only: `GraceElapsed` is enabled after the process exits | `worker_ownership.py:9-20` |
| `_DRAIN_GRACE_S` 60 | Drain completion is nondeterministic | `bot.py:233` |
| `docker stop` timeout 95 | SIGKILL is nondeterministic after SIGTERM | `deploy.yml:372` |
| Clock and durations | None; only orderings | Time-based claims are assumptions A5 and A6. |

### Assumptions

| # | Assumption | Status |
| --- | --- | --- |
| A1 | Discord drops a gateway session when the process closes it or dies, and a new session is not refused or delayed by the old one. | Unsupported: no staging contract test. Discord also rate-limits IDENTIFY; the model has no such delay. |
| A2 | `docker stop` sends SIGTERM, waits the timeout, then SIGKILLs; `up -d` returns before the new container has built its runtime. | Docker documented behaviour; the second half is the reason for `GatewayNoWaitReady`. |
| A3 | PostgreSQL releases session advisory locks when the backend connection ends, immediately. | PostgreSQL documented. Detection delay on the server for a half-open TCP connection (TCP keepalive) is not modelled: a lock may be held longer than the holder lives, which is safe. |
| A4 | The holder is the only writer of the lease row while it holds the gateway lock. | Designed. |
| A5 | A disconnected holder stops (`os._exit`) before its heartbeat ages past `OWNER_LOSS_GRACE_S`. | Code comment `worker_ownership.py:5-8` says this bound is required. Not replayed at real clock duration. `GatewayShortGrace` shows what happens without it. |
| A6 | A process that stays connected to Discord after its owner connection dies is stopped by the probe within the bound in A5, even if the event loop is slow. | Unsupported under event-loop stalls. |
| A7 | The designed recovery path reacquires the gateway lock, or fail-stops if another process holds it. | Designed; must be implemented. |

### Not modelled

- Poll interval for `pg_try_advisory_lock`: the gap after Old unlocks is at least one poll. A blocking `pg_advisory_lock` or a short interval keeps it small.
- Connection loss on a process that has not connected yet (it would simply re-poll).
- In-flight turns and card edits on Old continuing while New connects. Those are the overlap models in `adapter_recovery/` and `discord_startup_replay/`.
- Discord-side session resume, IDENTIFY rate limits and duplicate events.
- Failure of `Build` (New crashes on start): Old stays connected, which is safe, but the deploy then has no rollback logic here.

## Results

TLC 2.19 (tla2tools v1.7.4), one worker, from `formal/check.sh`.

| Config | Verdict | States | Meaning |
| --- | --- | ---: | --- |
| `GatewaySafe` | clean | 704 | Designed protocol with a readiness-gated deploy, one loss, one SIGKILL: `AtMostOneHandler`, `GapBounded`. |
| `GatewayProgress` | clean | 704 | `NewEventuallyConnects` under weak fairness, including after a loss or SIGKILL. |
| `GatewayThree` | clean | 8638 | Three processes: `AtMostOneHandler`. |
| `GatewayTrustLockNoLoss` | clean | 112 | Without a connection loss the lock alone is enough. |
| `GatewayUnlockBeforeCloseAccepting` | clean | 1574 | Early release still leaves one process accepting new work. |
| `GatewayStopFirst` | violates `GapBounded` | 8 | Today's deploy: the gap includes New's start-up. |
| `GatewayNoWaitReady` | violates `GapBounded` | 26 | Designed deploy without a readiness gate: same gap. |
| `GatewayUnlockBeforeClose` | violates `AtMostOneHandler` | 252 | Lease released before the gateway closes. |
| `GatewayTrustLockOnly` | violates `AtMostOneHandler` | 33 | Lock without lease check, with a connection loss. |
| `GatewayShortGrace` | violates `AtMostOneHandler` | 208 | Heartbeat grace can elapse before the holder stops. |
| `GatewayStaleRelease` | violates `AtMostOneHandler` | 2244 | `released_at` not cleared when taking the lease. |
| `GatewayWitnessLoss` | violates `NoUncleanHandover` | 203 | Reachable: New connects after Old's connection is lost. |
| `GatewayWitnessKill` | violates `NoKillHandover` | 186 | Reachable: New connects after Old is SIGKILLed. |
| `GatewayWitnessRecovery` | violates `NoRecovery` | 7 | Reachable: Old recovers its connection and keeps serving. |
| `GatewayWitnessWait` | violates `NoWaiting` | 33 | Reachable: New finds an unreleased holder and waits. |

## Counterexamples

**`GatewayStopFirst` (today's deploy).** Old is connected. The deploy stops it first. Old drains, closes the gateway and nothing is connected. New has not started yet, so the gap lasts through process start, imports and runtime build until New connects.

**`GatewayNoWaitReady` (the design as worded).** The deploy starts New, which is still building its runtime. It sends SIGTERM to Old straight away. With no in-flight turns Old drains at once and closes the gateway. New is not ready, so the gap again includes New's build. The fix is a readiness gate: `docker stop` Old only after New's liveness responder is up (`__main__.py:68`, bound after `build_runtime`) or its lease poll has started.

**`GatewayUnlockBeforeClose`.** Old receives SIGTERM and finishes draining. It writes `released_at` and unlocks the lease, then is about to close the gateway. New gets the lock, reads `released_at`, takes the lease and connects. Old is still connected: two processes are on the gateway. New accepts mentions while the draining Old still receives events, edits cards and handles interactions. Only new-mention acceptance stays exclusive (`GatewayUnlockBeforeCloseAccepting`).

**`GatewayTrustLockOnly`.** Old's owner connection is dropped by PostgreSQL, so the lock vanishes. Old does not know yet: it stays on the gateway until its next probe fails, and then for the retry budget and drain. New starts, gets the free lock and connects at once, without reading the lease or checking `owner_is_alive`. Both are connected.

**`GatewayShortGrace`.** Same loss. The heartbeat grace expires while Old, which has not stopped yet, is still connected. New holds the lock, sees Old not alive and connects. The grace has to outlast the whole fail-stop path (A5).

**`GatewayStaleRelease`.** Old releases cleanly and sets `released_at`. New takes the lease and connects but leaves `released_at` set. New's connection is dropped. A third process gets the lock, reads the old `released_at`, and connects while New is still on the gateway. The holder must clear `released_at` when it takes the lease.

## Replay status

None of the following has been done: counterexample tests against the real code (the lease does not exist yet), a Discord staging contract test for A1, and trace validation. The `GatewayNoWaitReady` counterexample can be replayed on the deploy script once it exists by delaying New's start-up and checking Old's close time. The `GatewayTrustLockOnly` and `GatewayShortGrace` orderings can be replayed against PostgreSQL with `pg_terminate_backend` on only the holder's backend, as `discord_startup_replay/` does.
