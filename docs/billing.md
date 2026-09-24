# Billing and credits

One deployment runs on one Anthropic API key, and any number of Discord
servers and Slack workspaces can install against it. The credit model is what
makes that safe: every model call is priced in USD and debited from the
tenant that caused it, and a turn cannot start unless that tenant has credit
left.

Metering and the balance gate work out of the box with no configuration.
Stripe top-ups and the per-person monthly cap need setup — see
[settings](#settings-that-turn-things-on) below.

## The unit of metering is a model call, not a turn

One agent turn fans out into many model calls. The driver meters each one:
`packages/core/daimon/core/turn/driver.py` matches on the turn's billing
posture and, for every `span.model_request_end` event on the stream, awaits
the recorder bound to that turn. This is the one piece of I/O permitted inline
in the consume loop, because an unmetered event is revenue lost and the
recorder is fail-closed — an exception propagates rather than being swallowed.

The posture is a union in `packages/core/daimon/core/turn/posture.py`:
`Billed(record=...)` or `BillingExempt(reason=...)`. There is deliberately no
no-op recorder, so a caller must say in the type which one it is.

`packages/core/daimon/core/usage_recording.py` is the recorder. Per model call
it writes two rows in one transaction:

- a **`usage_events`** row — token counts, model, tenant, platform user,
  session id, event id — carrying no cost column at all. Cost is computed at
  read time against current prices, so a repricing is a query change and never
  a backfill.
- a **`tenant_ledger`** row with the negative dollar delta.

Both are idempotent at the same grain. `usage_events` has a unique constraint
on `(managed_session_id, event_id)` and inserts on-conflict-do-nothing, and
the ledger entry's idempotency key is built from the same pair, so an SSE
replay costs nothing. A turn with no tenant behind it (a direct message)
records nothing and is debited nothing.

Model calls made by tools rather than by the agent's own turn go through the
same module with their own ledger reasons, so nothing that calls a model
escapes the ledger.

## Pricing

`packages/core/daimon/core/pricing.py` holds `MODEL_PRICING`, a table of
`ModelRates` in **USD per million tokens** with exactly four dimensions:
input, output, cache write and cache read. `cost_of` multiplies the event's
four token counts by those rates and returns USD. The table is split in two:
`AGENT_MODEL_PRICING` doubles as the allowlist of models an agent may be
configured with (`ALLOWED_MODEL_IDS` in
`packages/core/daimon/core/constants.py` is literally its keys), while
`TOOL_MODEL_PRICING` covers models pinned by individual MCP tools and never
selectable by an agent.

Opus 5.5 is priced at $4 input, $20 output, $5 five-minute cache write, and
$0.20 cache read per million tokens. The four-field ledger cannot distinguish
one-hour cache writes, which Anthropic prices at $8 per million tokens; it
currently treats all cache writes as five-minute writes.

Two consequences worth knowing before you add a model:

- **A model with no pricing row runs free.** `cost_of` returns `None`, the
  debit is `Decimal("0")`, and the `usage_events` row is still written. The
  only signal is a loud `billing.unpriced_model` warning, logged because the
  operator is giving compute away until it is fixed. Nothing fails closed.
- **Only token dimensions are priced.** The usage payload carries no
  server-tool counters, so spend on provider-side tools is not represented in
  `ModelRates` at all.

`DAIMON_BILLING__MARKUP` (default `1.0`, pass-through) multiplies the cost
before it is debited, in `debit_amount` in
`packages/core/daimon/core/tenant_balance.py`, quantized to six decimal
places. It is applied to the ledger only. Every reporting surface reprices raw
`usage_events` rows, so what a panel shows is provider cost, not what the
tenant was charged.

## The gates, in order

Two gates run before a turn starts, always balance then cap. For chat they
live in `packages/core/daimon/core/turn/admission.py`; the gate order there is
load-bearing and documented as such.

**Balance** — `is_over_balance` in
`packages/core/daimon/core/tenant_balance.py` sums the tenant's ledger and
denies unless the balance is strictly positive. It is independent of any
payment configuration: a deployment with no Stripe setup still refuses a
tenant whose trial credit is spent.

**Cap** — `is_over_cap` in `packages/core/daimon/core/billing.py` compares the
person's spend in the current calendar month, UTC, against their effective
cap. Caps live in `tenant_user_caps`, one row per `(tenant, platform user)`,
with a null-user row acting as the tenant's default. The cap is therefore
**per person**, defaulted tenant-wide; there is no aggregate tenant cap, no
per-session cap and no window other than the calendar month.

A denial raises `AdmissionDenied` carrying only the reason literal
(`balance_depleted` or `cap_exceeded`); the wording belongs to each adapter.
The turn aborts — it never silently degrades to a cheaper model. The same two
checks are re-run, with the same order, by the MCP tools that start a turn
(`_admit` in `packages/adapters/mcp/daimon/adapters/mcp/tools/_ctx.py`) and by
each scheduled routine fire, which records `balance_depleted` or
`cap_exceeded` as that run's error instead of raising. Discord's unprompted
thread participation checks too, and skips silently, on the grounds that a
billing notice is owed to someone who actually asked.

Two boundaries of the design worth stating plainly:

- **The gates run once, before the turn; debits land per model call.** Nothing
  re-checks mid-turn, so a single long turn can take a tenant's balance
  negative. The ledger allows it.
- **A caller with no platform user identity is not gated and not billed** — an
  operator or CLI token runs with no balance check, no cap check, no usage row
  and no debit. That is deliberate, and the module asks in as many words that
  no one add a fallback that bills it.

## The signup credit

`DAIMON_BILLING__SIGNUP_CREDIT` (default `10.00` USD) is seeded as a ledger
entry with reason `trial` when a tenant is provisioned, in `provision_tenant`
in `packages/core/daimon/core/defaults/provisioning.py`. It is granted **once
per tenant, not per person**: the idempotency key is derived from the tenant
id and the ledger's unique index on that key makes a re-provision — a rejoin,
a restart backfill, a self-heal — a no-op. Because tenant ids are derived from
`(platform, workspace id)` rather than allocated, a reinstall cannot mint a
second trial either.

Set it to `0` to require payment before use; a freshly installed tenant is
then balance-gated on its first message.

## Top-ups

The product flow is Stripe Checkout, one-time payments rather than a
subscription:

1. `/billing` on Discord or Slack offers fixed amounts, each labelled with an
   estimated number of turns derived from that tenant's own history. Admin
   status is verified at click time, not at render.
2. The chat adapters never import `stripe`. They mint a token and POST to
   `/billing/checkout` on the MCP server
   (`packages/adapters/mcp/daimon/adapters/mcp/checkout.py`), which takes the
   tenant from the verified token claim and never from the request body.
3. `packages/adapters/mcp/daimon/adapters/mcp/webhooks.py` handles the
   callback. It takes the amount from Stripe's own `amount_total` rather than
   from metadata, dedups on the Stripe event id in `payment_events`, claims
   the credit with a compare-and-set on `credited_at`, and only then inserts
   the positive ledger entry. Refunds and disputes insert compensating
   negative entries against a cumulative high-water mark per payment intent,
   so a refund followed by a dispute on one charge cannot claw back twice.
   Each clawback locks the original credit row before it reads that mark, so
   the guarantee also holds when Stripe delivers the two events at once.

Both the checkout and webhook routes are mounted only when Stripe is
configured. A self-hoster without it credits a tenant by inserting a
`tenant_ledger` row directly with a positive `delta_usd` and a unique
idempotency key — `.env.example` spells this out, and the unique index makes
a re-run harmless.

There is no CLI command and no MCP tool that adds credit.

## The tables

| Table | Holds |
| --- | --- |
| `tenant_ledger` | every credit and debit, append-only. **Balance is `SUM(delta_usd)`, never a column.** A unique index on `idempotency_key` is the entire idempotency story. |
| `usage_events` | token counts per model call. No money column; cost is computed on read. |
| `payment_events` | Stripe webhook dedup, keyed by the Stripe event id, with the compare-and-set `credited_at`. Explicitly not a ledger. |
| `tenant_user_caps` | per-person monthly caps, with a null-user row as the tenant default. |

All four are declared in `packages/core/daimon/core/_models.py` with stores
beside them in `packages/core/daimon/core/stores/`. Ledger reasons in use:
`trial`, `topup`, `turn_debit`, `checkpoint_debit`, `media_debit`,
`classifier_debit`, `thread_naming_debit`, and the two clawback reasons named
after their Stripe events.

**The sweep.** An MCP `start_turn` creates a session and sends a message but
never drives the stream, so the inline hook never fires for it.
`packages/core/daimon/core/usage_sweep.py` closes that hole: each scheduler
tick it walks Managed Agents sessions, skips any without a `daimon_tenant`
stamp or belonging to a tenant this deployment does not own, and replays their
`span.model_request_end` events through the same recorder. It is safe to run
against already-metered sessions precisely because the idempotency grain is
the same.

## Settings that turn things on

Policy is nested under `DAIMON_BILLING__`; the Stripe secrets are flat and
deliberately kept separate. Both blocks are catalogued in
[configuration.md](configuration.md#billing-policy) and
[configuration.md](configuration.md#billing-stripe).

`load_billing_config` in `packages/core/daimon/core/billing.py` requires
**all** of the flat Stripe variables together. If any one is missing it logs
that billing is disabled, returns `None`, and three things follow:

- the checkout, webhook and landing routes are not mounted;
- **the cap gate becomes inert** — `is_over_cap` returns `False` immediately
  when there is no billing config, so monthly caps do nothing at all;
- metering, debits, the balance gate and the trial credit are unaffected.

Self-service top-ups additionally need `DAIMON_MCP__PUBLIC_URL` and
`DAIMON_MCP__JWT_SECRET` for the adapter-to-MCP hop, and the image must carry
the optional `billing` extra, which is what pulls in `stripe`.

One gap to be aware of when reading the cap code: nothing in the shipped
adapters or CLI writes a `tenant_user_caps` row. The stores exist and the gate
reads them, but today a cap has to be inserted directly, and some user-facing
copy still says "when available" for exactly that reason.

## What you can see

`/billing` on Discord and Slack is the reporting surface, always over the
current calendar month, built from
`packages/core/daimon/core/stores/usage_events.py`. A member sees their own
spend, turn count and cap plus the tenant balance; an admin additionally sees
tenant totals and a per-member breakdown. Admin-only figures are not fetched
for a non-admin rather than fetched and hidden.

Two things those numbers are not. They are pre-markup, as above. And tenant
aggregates exclude rows with no platform user attached, so spend recovered by
the sweep from a session with no account stamp is debited to the ledger but
does not appear in the panel's tenant total.

For a single finished turn there is `get_turn_cost` in
`packages/adapters/mcp/daimon/adapters/mcp/tools/agent_chat.py`, which folds
that turn's events into a raw pre-markup figure and returns it as a decimal
string — never a float, and `None` rather than `0` for an unpriced model,
because zero would falsely claim the turn was free.
