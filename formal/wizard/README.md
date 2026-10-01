# Wizard lifecycle model

Set `TLA2TOOLS_JAR` to a local TLC 2.19+ jar and run from the repository root.
Each command uses `java` from `PATH` and stores state metadata under the
temporary directory. The unsafe configurations are expected to report an
invariant violation; the safe and progress configurations must pass.

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to the path of tla2tools.jar}"
WIZARD_TLC_DIR="${TMPDIR:-/tmp}/daimon-wizard-tlc"
mkdir -p "$WIZARD_TLC_DIR/safe" "$WIZARD_TLC_DIR/progress"
java -jar "$TLA2TOOLS_JAR" -metadir "$WIZARD_TLC_DIR/safe" -config formal/wizard/WizardLifecycle.cfg formal/wizard/WizardLifecycle.tla
java -jar "$TLA2TOOLS_JAR" -metadir "$WIZARD_TLC_DIR/progress" -config formal/wizard/WizardLifecycleProgress.cfg formal/wizard/WizardLifecycle.tla
```

To replay the two pre-fix counterexamples, run these separately; exit status 12
is expected because TLC detects the named invariant violation:

```sh
java -jar "$TLA2TOOLS_JAR" -metadir "$WIZARD_TLC_DIR/stale-submit" -config formal/wizard/WizardStaleSubmit.cfg formal/wizard/WizardLifecycle.tla
java -jar "$TLA2TOOLS_JAR" -metadir "$WIZARD_TLC_DIR/sweep-race" -config formal/wizard/WizardSweepRace.cfg formal/wizard/WizardLifecycle.tla
```

## Source mapping and bounds

The model follows one persisted wizard row and one value representing its
answer set. `answersVersion` models the row's answer revision; the production
token is `updated_at`, which every state write must advance. `ReadSubmit` maps
to `_load_row` and the `WizardState` reconstruction in
`packages/adapters/discord/daimon/adapters/discord/wizard_submit.py`;
`CaptureSubmitTime` maps to the callback's injected `now` passed to
`try_claim_submit`. `EditAnswers` maps to
`update_wizard_state`'s conditional update in
`packages/core/daimon/core/stores/wizard_session.py`. `BeginSubmit` and
`CommitSubmit` split that store's single conditional `UPDATE ... RETURNING`
from transaction commit and subsequent `_spawn` in the adapter, so the model
can interleave the sweep's read with an uncommitted submit.

`Expire` abstracts wall-clock passage past `expires_at`.
`SelectExpired` and `MarkAbandoned` map to the candidate-ID `SELECT` and
follow-up `UPDATE` in `abandon_expired_wizard_sessions`, called by
`packages/core/daimon/core/wizard_sweep.py`. PostgreSQL READ COMMITTED behavior
is abstracted as the candidate select observing the previous committed open
version while a submit holds an uncommitted row update. `submitInFlight`
models that row lock; the sweep update waits for it to settle.

The finite bound allows one edit, two competing submitters, one expiry
transition, one sweep candidate, and at most one turn start. It checks the
stale submit/edit ordering, a second submit waiting for the first claim, and a
pre-expiry submit overlapping the post-expiry sweep. It abstracts actual
answer payloads, multiple rows, button authorization, Discord edits, upstream
turn completion/billing, database/network failures,
transaction rollback, and process death between submit commit and `_spawn`.
The model uses `updated_at` as a revision token and assumes the store advances
it strictly on every mutation.

## Findings and fixes

Both unsafe configurations produced reachable counterexamples before the
fixes. They model the former store predicates; the safe configuration models
the predicates now present in source.

1. **Stale submit replaced a newer edit.** `WizardStaleSubmit.cfg` violated
   `SubmittedUsesCurrentAnswers` along this trace:

   `ReadSubmit → EditAnswers → CaptureSubmitTime → BeginSubmit → CommitSubmit`

   The submit wrote its snapshot version `v0` after the row had advanced to
   `v1`. `try_claim_submit` now compares `updated_at` to the callback's observed
   token. `update_wizard_state` and the submit claim use `GREATEST(now,
   updated_at + 1 microsecond)` so even equal clock readings advance the token.
   A failed claim reloads the latest row; an open edit remains interactive.
   Real-Postgres store regression and Discord callback regression tests cover
   the stale path.

2. **Expiry sweep downgraded an in-flight submit.** `WizardSweepRace.cfg`
   violated `SubmittedNeverDowngrades` along this trace:

   `ReadSubmit → CaptureSubmitTime → BeginSubmit → Expire → SelectExpired → CommitSubmit → MarkAbandoned`

   The sweep selected the still-committed open row while submit's update was
   uncommitted, then its ID-only update changed the submitted row after the
   submit committed. The final sweep update now repeats `status = 'open'` and
   `expires_at <= now`, so the stale candidate no longer matches. A real
   two-connection Postgres regression pauses between the sweep's select and
   update to reproduce this ordering.

TLC 2.19 passed the safe safety and progress configurations after the fixes:
232 states generated, 92 distinct states, depth 11. The safe checks cover
`TypeOK`, current-answer consistency, terminal-state monotonicity, at-most-one
turn start, submit settlement, second-submit loss, and expiry settlement under
weak fairness for submit commit, second-submit rejection, and sweep
selection/update. These finite checks support the
source-grounded predicates; they do not prove the Python, SQLAlchemy,
PostgreSQL, Discord, or Managed Agents implementation correct.
