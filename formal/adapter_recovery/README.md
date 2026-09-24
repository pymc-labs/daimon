# Adapter active-turn recovery

Run from the repository root with a JRE and TLA+ tools jar:

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar}"
MODEL_DIR="${TMPDIR:-/tmp}/daimon-adapter-recovery-tlc"
mkdir -p "$MODEL_DIR/discord-unsafe" "$MODEL_DIR/slack-unsafe" "$MODEL_DIR/fixed"
# Both expected invariant failures return 12 and print a counterexample trace.
java -jar "$TLA2TOOLS_JAR" -metadir "$MODEL_DIR/discord-unsafe" -config formal/adapter_recovery/DiscordUnsafe.cfg formal/adapter_recovery/AdapterRecovery.tla || test "$?" -eq 12
java -jar "$TLA2TOOLS_JAR" -metadir "$MODEL_DIR/slack-unsafe" -config formal/adapter_recovery/SlackUnsafe.cfg formal/adapter_recovery/AdapterRecovery.tla || test "$?" -eq 12
java -jar "$TLA2TOOLS_JAR" -metadir "$MODEL_DIR/fixed" -config formal/adapter_recovery/AdapterRecovery.cfg formal/adapter_recovery/AdapterRecovery.tla
```

The model checks the durable per-thread active-card marker against an old
process's boot snapshot. `TakeSnapshot` reads the marker row. `StartTurn`
registers a fresh status card and makes it live. `ClearSnapshot` retires the
old card and conditionally clears the marker. `FinishTurn` is normal terminal
cleanup. The finite domain contains one pre-existing marker and one new turn.

| Model item | Implementation |
| --- | --- |
| `TakeSnapshot`, `ClearSnapshot` | Discord `_retire_orphaned_turns_once` in [`bot.py`](../../packages/adapters/discord/daimon/adapters/discord/bot.py:598); Slack `retire_orphaned_turns` in [`boot_sweep.py`](../../packages/adapters/slack/daimon/adapters/slack/boot_sweep.py:217) |
| `UseCAS` | Discord's `clear_active_turn_if_message_id` compare-and-clear; Slack already used that store operation |
| `StartTurn`, `FinishTurn` | Adapter status-card lifecycle and active marker writes/cleanup; Discord wizard submissions also mark and clear in [`wizard_submit.py`](../../packages/adapters/discord/daimon/adapters/discord/wizard_submit.py:341) |
| `AllowTurnBeforeRecovery` | Slack mention and continuation admission in [`app.py`](../../packages/adapters/slack/daimon/adapters/slack/app.py:807), Discord `_orchestrate` and continuation dispatch in [`bot.py`](../../packages/adapters/discord/daimon/adapters/discord/bot.py:1343) |

## Bounds and assumptions

- One thread, one stale marker, one newly admitted turn, and one recovery pass
  suffice to expose stale-snapshot deletion. Platform message IDs are distinct
  (`OldMarker` vs `NewMarker`); database writes and remote edits are atomic
  model actions.
- `LiveTurnHasMarker` is the checked safety condition. No fairness or progress
  claim is made: platform calls and recovery can fail or hang.
- The model abstracts the first status post and marker write into `StartTurn`.
  It does not model process death or an ambiguous platform response.
- A passing TLC run validates only this finite transition abstraction, not the
  Python implementations, database isolation, or platform APIs.

## Findings and traces

Both unsafe configurations violate `LiveTurnHasMarker`. Discord's previous
unconditional clear admits this trace: start the new turn, snapshot its new
marker as if orphaned, then clear it unconditionally. Slack's existing CAS
alone is not sufficient: if a turn writes its marker before the boot snapshot,
the snapshot records the new marker and the CAS still matches. The executable
fix is to keep turn admission behind recovery completion in both adapters;
the Slack entrypoint starts recovery before connecting, and turn/continuation
handlers await that task. Discord serializes reconnect recovery with turn
admission and uses compare-and-clear so a marker changed after the snapshot
survives. The fixed configuration has no reachable live-turn marker loss.

There remains a separate crash window: a process can post its initial status
card and die before persisting the marker. Boot recovery cannot discover that
card from the current database schema. Closing this window safely requires a
durable pre-post intent or changing post/bind ordering; either changes recovery
or user-visible latency semantics and is outside this minimal repair. Recovery
also assumes a single adapter process; overlapping old and new instances can
misclassify the sibling's marker as an orphan.

## TLC evidence

Checked with TLC 2.19 and Java 21. Discord and Slack unsafe configs each
generate 8 states / 8 distinct states before the expected
`LiveTurnHasMarker` violation. The trace is `StartTurn` → `TakeSnapshot`
(`NewMarker`) → `ClearSnapshot` (marker becomes `NoMarker` while the turn is
live). The fixed config generates 5 states / 4 distinct states and passes
`TypeOK` and `LiveTurnHasMarker`.

## Adapter restart, overlapping processes and the MA session (`AdapterOverlap.tla`)

`AdapterRecovery.tla` above stops at one marker and one process.
`AdapterOverlap.tla` adds the pieces its bounds leave out: an old and a new
adapter process, thread_sessions rows with their markers, each row's MA session
status (idle / running / dead), the bind lock released before the marker is
written, dead-session recovery outside that lock, and the Discord wizard turn
that skips `_processing`.

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar}"
cd formal/adapter_recovery
run() { java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -metadir "${TMPDIR:-/tmp}/daimon-adapter-overlap-tlc/$1" -config "$1.cfg" AdapterOverlap.tla; }
for c in OverlapCASNoStale OverlapGated OrphanSweepInterrupts OrphanSendWaits WizardBypassRowsLocked SingleProcessRecovery; do run $c; done
for c in OverlapPreCAS OverlapCASOnly OrphanedTurnReuse RollingOverlap WizardBypassMessage WizardBypassRows; do run $c || test $? -eq 12; done
```

| Model item | Implementation |
| --- | --- |
| `OldDies`, `StartNew` | container stop after the drain window (Slack 50 s, Discord 60 s; neither interrupts MA) or a crash; the replacement container boots |
| `SweepSnap`, `SweepClear` | Slack `retire_orphaned_turns`, Discord `_retire_orphaned_turns_once` (list marked rows, retire the card, compare-and-clear) |
| `Admit` | per-thread `_processing` and the recovery gate; `WizardBypass` is Discord's `wizard_submit` (documented to skip `_processing`) |
| `Bind` | `prepare_session_for_turn` under `pg_advisory_xact_lock`; a plain reuse reads neither the marker nor MA's session status |
| `Mark` | the adapter writes the active-turn marker after bind returns |
| `Send`, `Observe` | driver opens the stream and sends `user.message`; MA answers 200 to a `user.*` event sent into a running session and ignores it ([`driver.py`](../../packages/core/daimon/core/turn/driver.py), measured 2026-08-26) |
| `Rec1`–`Rec3` | [`run.py`](../../packages/core/daimon/core/turn/run.py) `mark_dead` → `create_fresh_session` → `link_replacement`, outside the bind lock |
| `Finish` | unconditional `clear_active_turn` on the turn's own row |

Invariants: `AtMostOneLiveRow` (≤1 live thread_sessions row for the thread),
`NoMessageIntoRunning` (no `user.message` into a session still running a turn),
`NoStolenClear` (a marker is cleared only by the turn that set it, or once that
turn's process is gone), and `NoStaleClear` (the sweep never clears a live
turn's marker written after the sweep's own snapshot). `NoStaleClear` is the
narrower property: compare-and-clear alone guarantees it, but not
`NoStolenClear`, because a live marker written before the snapshot still
matches.

### Can adapter processes overlap in the hosted deploy?

Not as deployed. [`deploy.yml`](../../.github/workflows/deploy.yml) refreshes the
worker VM with `docker compose -f compose.worker.yml up -d`, which recreates a
changed service by stopping the old container before starting its replacement,
and its health gate addresses a single `daimon-slack-1` container. Self-hosted
[`docker-compose.yml`](../../docker-compose.yml) runs one container per adapter.
The worker compose file itself lives in the operator's private repository, so
its replica count and stop timeout cannot be checked here; the code assumes one
process per platform ([`thread_sessions.py`](../../packages/core/daimon/core/stores/thread_sessions.py)
`list_orphaned_turns`, Slack `boot_sweep.py`). The `mcp` service is on Cloud Run
and can run several instances, but it runs OAuth callbacks and MCP tools, not
thread turns.

So `Overlap = FALSE` is the modelled deployment. `RollingOverlap` records what
breaks if that assumption changes (a start-first rollout, a second replica, or
an operator starting a second container): the new process's sweep clears the
sibling's live marker even with compare-and-clear, the recovery gate and the
MA interrupt. An owner id on the marker is needed before scaling out.

What does overlap without a second process:

1. **The MA session outlives the process.** A turn longer than the drain window
   keeps running on MA after its container stops.
2. **Discord wizard turns skip `_processing`** in the same process.

### Calibration

| Fix | Config | Verdict |
| --- | --- | --- |
| `287b719` base marker, unconditional sweep clear, turns admitted before the sweep (Slack until `2d3a002`, Discord until `c923083`) | `OverlapPreCAS` | violates `NoStaleClear` |
| `0bc1b14`/`2d3a002` compare-and-clear | `OverlapCASNoStale` | clean (`NoStaleClear`) |
| compare-and-clear alone, still admitted before the sweep (before `c923083`) | `OverlapCASOnly` | violates `NoStolenClear` |
| turn admission waits for orphan recovery: `c923083` for Slack; for Discord `c923083` gated only once the gateway was ready, and the full gate is pymc-labs/daimon#232 (sweep armed in `setup_hook`, every turn entry point awaits it) | `OverlapGated` | clean (`NoStolenClear`, `AtMostOneLiveRow`) |

The two violating traces differ. `OverlapPreCAS` (11 steps, the race
`2d3a002`'s docstring describes): the old process marks a turn and dies → the
new process starts and its sweep snapshots the orphan's marker → a mention is
admitted before recovery, reuses the row and writes its own marker → the sweep
clears unconditionally and wipes the live marker. With `SweepCAS = TRUE` the
same configuration is clean (`OverlapCASNoStale`). `OverlapCASOnly` (8 steps,
the case `c923083`'s README gives for Slack): the new process admits a turn and
writes its marker before the sweep's snapshot, so the snapshot holds the live
marker and the compare-and-clear still matches.

`8282714` (retry a failed sweep) is a liveness repair; this safety model does not
cover it.

### Regression row (fixed on main) and accepted limitations

- **A message sent after a restart was ignored by the still-running orphan**
  (`OrphanedTurnReuse`, violates `NoMessageIntoRunning`; fixed on main by
  pymc-labs/daimon#232). Trace before #232: the old process
  starts a turn → the container stops (MA keeps running the turn) → the new
  process boots → its sweep retires the card as interrupted and clears the
  marker, without interrupting MA → a new mention is admitted, reuses the row and
  sends `user.message` into the running session. MA ignores it, the new turn
  renders the orphan's remaining output as its reply, and the orphan keeps
  billing. Interrupting the orphan's session in the sweep (`OrphanSweepInterrupts`)
  or never sending into a running session (`OrphanSendWaits`) is clean. The model
  treats the interrupt as immediate; in the code a mention in the seconds before
  MA reaches idle can still meet a running session, and main's interrupt is
  best-effort with a 10 s timeout. `OrphanSweepInterrupts` matches main after
  #232.
- **Discord wizard turn and mention turn on one session** (`WizardBypassMessage`,
  violates `NoMessageIntoRunning`): the second `user.message` is ignored rather
  than run concurrently. `WizardBypassRows` (violates `AtMostOneLiveRow`): if the
  session dies while both are in flight, one turn's recovery marks the row dead
  while the other binds and creates a row, then the first creates another, which
  leaves two live rows (reads pick the newest; the other MA session is orphaned).
  `wizard_submit.py` documents the bypass as an accepted limitation; this is its
  cost. Recovery under the bind lock that adopts an existing replacement
  (`WizardBypassRowsLocked`) is clean. Not changed: product decision.
- `SingleProcessRecovery` (one process, one session death, sweep interrupt) is
  clean: without overlap or the wizard bypass, the in-process guard makes
  lock-free recovery safe.

### Bounds

Two processes (the old one can only die, the new one can only start), two
turns, up to four rows, at most one session death. DB transactions and MA calls
are atomic steps; platform card edits, cancel registration and reconnects are
omitted.
