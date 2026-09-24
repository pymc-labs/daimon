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

## Out-of-order delivery and missing-credit recovery

Stripe explicitly does not guarantee event delivery order, and it retries
failed deliveries for up to three days; operators can manually resend for up
to 30 days ([Stripe webhook guidance](https://docs.stripe.com/webhooks#event-ordering)).
`OutOfOrder.tla` abstracts two transactions for one payment intent:
Checkout completion creates the credit and drains retained events, while a
refund/dispute either debits an existing credit or persists a pending target.
The unprotected configuration finds an orphan pending event when each
transaction observes the other's pre-commit state. The locked configuration
serializes both paths and checks `NoOrphanPending`, `NoOverClawback`, and
`Progress`.

Run both models from the repository root (the unlocked run is expected to fail
with exit code 12):

```sh
set -eu
: "${TLA2TOOLS_JAR:?Set TLA2TOOLS_JAR to tla2tools.jar}"
TLA_META_DIR="${TMPDIR:-/tmp}/daimon-billing-tlc"
mkdir -p "$TLA_META_DIR/out-of-order-unlocked" "$TLA_META_DIR/out-of-order-locked"
java -jar "$TLA2TOOLS_JAR" -metadir "$TLA_META_DIR/out-of-order-unlocked" -config formal/billing/OutOfOrderUnlocked.cfg formal/billing/OutOfOrder.tla || test "$?" -eq 12
java -jar "$TLA2TOOLS_JAR" -metadir "$TLA_META_DIR/out-of-order-locked" -config formal/billing/OutOfOrderLocked.cfg formal/billing/OutOfOrder.tla
```

TLC 2.19 produced the expected unlocked counterexample in five states: Checkout
observes no pending event; clawback observes no credit; Checkout commits its
credit; clawback commits the pending event, which can no longer be drained by
that completion. With the lock enabled, TLC checks 13 states to depth 7, all
three invariants, and `Progress` with weak fairness on each transaction step.
The finite model uses a $1 credit and a full-value clawback. The executable
regressions use a $10 credit, sequential $4/$7 cumulative targets, and a
concurrent two-request delivery against real PostgreSQL. The test synchronizes
both deliveries before their lock calls but does not force a stale-read
interleaving if the lock implementation were removed; the unlocked TLC trace
models that interleaving directly, while the sequential webhook test verifies
durable retention and drain behavior.

| Model item | Implementation |
| --- | --- |
| `AcquireCheckout` / `AcquireClawback` | [`pending_clawbacks.lock_payment_intent`](../../packages/core/daimon/core/stores/pending_clawbacks.py), called before credit lookup/drain in [`webhooks.py`](../../packages/adapters/mcp/daimon/adapters/mcp/webhooks.py) |
| `ObservePending` / `CommitCheckout` | [`_handle_completed`](../../packages/adapters/mcp/daimon/adapters/mcp/webhooks.py), credit insert/idempotency via [`tenant_ledger.insert_entry`](../../packages/core/daimon/core/stores/tenant_ledger.py), followed by pending drain |
| `ObserveCredit` / no-credit `CommitClawback` | [`_handle_clawback`](../../packages/adapters/mcp/daimon/adapters/mcp/webhooks.py) and [`pending_clawbacks.enqueue`](../../packages/core/daimon/core/stores/pending_clawbacks.py) |
| Apply and remove retained event | [`_drain_pending_clawbacks`](../../packages/adapters/mcp/daimon/adapters/mcp/webhooks.py), [`_record_clawback_delta`](../../packages/adapters/mcp/daimon/adapters/mcp/webhooks.py), [`pending_clawbacks.remove`](../../packages/core/daimon/core/stores/pending_clawbacks.py) |
| Durable record | [`PendingPaymentClawback`](../../packages/core/daimon/core/_models.py), migration [`0024_pending_payment_clawbacks.py`](../../packages/core/alembic/versions/0024_pending_payment_clawbacks.py) |
| Executable sequential and concurrent cases | [`test_refund_before_completion_is_applied_when_credit_arrives`](../../packages/adapters/mcp/tests/test_webhooks_stripe.py), [`test_absent_credit_clawback_and_completion_concurrent_delivery`](../../packages/adapters/mcp/tests/test_webhooks_stripe.py) |
| Transaction rollback/retry case | [`test_pending_drain_failure_rolls_back_credit_and_keeps_event`](../../packages/adapters/mcp/tests/test_webhooks_stripe.py) |

Pending events have no tenant foreign key because the tenant is unknown until
a matching Checkout credit exists. Unmatched records expire after 90 days,
which exceeds Stripe's documented 30-day manual resend window. Expiry is
opportunistic: a later completion or clawback webhook removes expired records;
an otherwise idle deployment may retain expired rows until another billing
webhook arrives. Missing-payment-intent events cannot be associated safely and
are acknowledged/logged without retention. A checked-out credit with a
missing source payment event is retained as pending and logged rather than
debiting an unverified tenant. Existing duplicate topup rows for one payment
intent, if created before uniqueness was enforced, require reconciliation;
the new advisory lock prevents new clawback races but does not merge old rows.

Model assumptions: one matching credit per payment intent, verified webhook
payloads, valid bounded currency amounts, successful PostgreSQL transactions,
and eventual scheduling of the weakly fair steps. The model abstracts the
database lock as a single per-intent owner and successful drain as one atomic
commit step; SQL rollback, DB/network failures, cleanup scheduling, duplicate
event delivery, malformed payloads, historical duplicate credits, Stripe's
state, and unrelated ledger writes are not modeled. TLC checks only this finite
abstraction, not the implementation; signed webhook and real-Postgres
regression tests provide separate implementation evidence.

The same webhook review found that two distinct completion Event IDs could
credit one payment twice. Completion now uses the payment intent in the ledger
idempotency key, checks older event-keyed credit rows before inserting, and
validates any conflicting credit's tenant and amount. Sequential, concurrent,
and legacy-row regressions cover this fix. Duplicate completion is outside
`Clawback.tla`; the model's one-credit assumption is enforced by those tests.
