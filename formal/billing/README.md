# Concurrent clawback model

Run from the repository root with a JRE and the pinned TLA+ tools jar described
in the [coverage report](../README.md):

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to tla2tools.jar}"
TLC_META_DIR="${TMPDIR:-/tmp}/daimon-billing-tlc"
mkdir -p "$TLC_META_DIR/unlocked" "$TLC_META_DIR/locked"
# The unlocked configuration intentionally finds a counterexample (exit 12).
java -jar "$TLA2TOOLS_JAR" -metadir "$TLC_META_DIR/unlocked" -config formal/billing/ClawbackUnlocked.cfg formal/billing/Clawback.tla || test "$?" -eq 12
java -jar "$TLA2TOOLS_JAR" -metadir "$TLC_META_DIR/locked" -config formal/billing/ClawbackLocked.cfg formal/billing/Clawback.tla
```

The finite model has one original $1 credit and two distinct, full-value
callbacks (refund and dispute). `clawedBack` is the total magnitude of negative
ledger entries. Each callback reads it, computes `max(0, 1 - observedTotal)`,
then commits a distinct row. The read and commit are separate model actions to
allow concurrent transactions. A boolean `UseCreditLock` chooses whether each
callback must lock the common original credit row until commit.

| Model item | Implementation |
| --- | --- |
| Original credit lookup / `LockCredit` | [`tenant_ledger.get_by_payment_intent`](../../packages/core/daimon/core/stores/tenant_ledger.py:85), called with `for_update=True` by [`_handle_clawback`](../../packages/adapters/mcp/daimon/adapters/mcp/webhooks.py:471) |
| `ReadCumulative` / `observedTotal` | [`tenant_ledger.get_clawed_back_total`](../../packages/core/daimon/core/stores/tenant_ledger.py:61) |
| `CommitClawback` / `clawedBack` | [`_handle_clawback`](../../packages/adapters/mcp/daimon/adapters/mcp/webhooks.py:531) and [`tenant_ledger.insert_entry`](../../packages/core/daimon/core/stores/tenant_ledger.py:23) |
| Distinct event IDs and transaction boundary | [`_handle_clawback`](../../packages/adapters/mcp/daimon/adapters/mcp/webhooks.py:494), [`payment_events`](../../packages/core/daimon/core/stores/payment_events.py) |
| Executable interleaving regression | [`test_concurrent_clawbacks_cannot_exceed_original_credit`](../../packages/adapters/mcp/tests/test_webhooks_stripe.py) |

`ClawbackUnlocked.cfg` finds `NeverOverClawback` false in five states:
both callbacks read zero, then each commits a $1 debit; `clawedBack = 2`.
The Postgres regression reproduced the corresponding $10 credit becoming a
negative $10 balance before the lock. `ClawbackLocked.cfg` checks `TypeOK`,
`NeverOverClawback`, and eventual completion under weak fairness of each
enabled callback action. TLC 2.19 explores 13 distinct states to depth 7 and
reports no error. The second callback reads the first committed debit and
adds zero. PostgreSQL row locking serializes the transactions on the same
credit row, and a later `READ COMMITTED` query sees the first committed debit.

The model assumes one credit row per payment intent, two valid callbacks,
successful transactions, PostgreSQL `READ COMMITTED` isolation, finite work,
and weak fairness for progress. It abstracts SQL conflict handling, duplicate
delivery of the same event ID, rollback, callback arrival before credit,
partial refunds, connection failure, payment-provider state, and all other
ledger writes. TLC checks these abstract states, not the Python or database
implementation. The concurrent Postgres regression covers the relevant
implementation interleaving separately.

The same webhook review found that two distinct completion Event IDs could
credit one payment twice. Completion now uses the payment intent in the ledger
idempotency key, checks older event-keyed credit rows before inserting, and
validates any conflicting credit's tenant and amount. Sequential, concurrent,
and legacy-row regressions cover this fix. Duplicate completion is outside
`Clawback.tla`; the model's one-credit assumption is enforced by those tests.
