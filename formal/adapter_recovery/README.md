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
