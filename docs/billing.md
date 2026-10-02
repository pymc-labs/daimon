# Billing and credits

One deployment runs on one Anthropic API key, and any number of Discord
servers and Slack workspaces can install against it. The credit model is what
makes that safe: every model call is priced in USD and debited from the
tenant that caused it. By default, a turn cannot start unless that tenant has
credit left. Operators can explicitly fund individual tenants themselves.

Metering and the balance gate work out of the box with no configuration.
Stripe top-ups and the per-person monthly cap need setup — see
[settings](#settings-that-turn-things-on) below. [Channel
budgets](#channel-budgets) are off until an admin sets one.

## Tenant funding mode

Every tenant defaults to `prepaid`, including existing tenants after migration.
Set one tenant's mode from the operator CLI:

```bash
daimon tenants funding-mode discord GUILD_ID operator_funded
daimon tenants funding-mode slack TEAM_ID prepaid
daimon tenants list --json
```

`operator_funded` permits turns at zero or negative balance. The provider bill
belongs to the operator. Each depleted-balance admission emits the structured
warning `billing.operator_funded_balance_alert` with `tenant_id` and
`balance_usd`; route this event through your log alerting system. There is no
chat notification or deduplication of these warnings.

Usage events, ledger debits, markup and configured monthly caps still apply.
The balance can become negative and remains visible as a spend signal. Funding
mode does not enable or disable caps: they retain the billing configuration
requirements described below. The policy applies to chat, MCP tools and routine
admission through the shared balance gate. Other tenants stay prepaid.

Switching back to `prepaid` immediately restores the positive-balance gate;
it does not reset the ledger or add credit. Inspect the balance before switching.

## The unit of metering is a model call, not a turn

One agent turn fans out into many model calls. The driver meters each one:
`packages/core/daimon/core/turn/driver.py` matches on the turn's billing
posture and, for every `span.model_request_end` event on the stream, awaits
the recorder bound to that turn. This is the one piece of I/O permitted inline
in the consume loop, because an unmetered event is revenue lost and the
recorder is fail-closed — an exception propagates rather than being swallowed.

A call MA makes while no stream is attached — after a dropped connection, a
server-side close or a read stall — reaches the driver only through the
history replay it runs before reconnecting or finalizing. The driver bills
the model calls in that replayed suffix through the same recorder, and a
per-turn set of billed event ids keeps every call to one recorder invocation
whether it arrives live, in a replay, or both. Calls made after an interrupt
or a turn ceiling, or while the adapter process is down, are not seen by the
driver at all; those are left to [the sweep](#the-tables).

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
selectable by an agent. Every agent-model row is the provider's list price.
Put any margin in `DAIMON_BILLING__MARKUP` (below), not in the table, because
reports read the table as provider cost.

Opus 5.5 is priced at $4 input, $20 output, $5 five-minute cache write, and
$0.20 cache read per million tokens; Sonnet 5.5 at $2, $10, $2.50 and $0.20.
The four-field ledger cannot distinguish one-hour cache writes, which
Anthropic prices at $8 (Opus 5.5) and $4 (Sonnet 5.5) per million tokens; it
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

Three gates run before a turn starts, always balance, then cap, then channel
budget. For chat they live in `packages/core/daimon/core/turn/admission.py`; the gate order there is
load-bearing and documented as such.

**Balance** — `is_over_balance` in
`packages/core/daimon/core/tenant_balance.py` sums the tenant's ledger and (for prepaid tenants)
denies unless the balance is strictly positive. It is independent of any
payment configuration: a deployment with no Stripe setup still refuses a
prepaid tenant whose trial credit is spent. Operator-funded tenants instead
emit the balance alert described above.

**Cap** — `is_over_cap` in `packages/core/daimon/core/billing.py` compares the
person's spend in the current calendar month, UTC, against their effective
cap. Caps live in `tenant_user_caps`, one row per `(tenant, platform user)`,
with a null-user row acting as the tenant's default. The cap is therefore
**per person**, defaulted tenant-wide; there is no aggregate tenant cap, no
per-session cap and no window other than the calendar month.

**Channel budget** — `is_over_channel_budget` in
`packages/core/daimon/core/channel_budget.py` compares a channel's spend in
its budget window against the budget's limit; see
[channel budgets](#channel-budgets). A DM moved with `/dm` counts toward the
channel it came from. A turn with no channel (an older DM, an MCP turn from
a key not minted in a channel) and a channel with no budget are never gated.

A denial raises `AdmissionDenied` carrying only the reason literal
(`balance_depleted`, `cap_exceeded` or `channel_budget_exceeded`); the wording
belongs to each adapter. The turn aborts — it never silently degrades to a
cheaper model. The balance and cap checks are re-run, with the same order, by
the MCP tools that start a turn (`_admit` in
`packages/adapters/mcp/daimon/adapters/mcp/tools/_ctx.py`), followed by the
budget of the channel an agent key was minted in; the billed media
tool (`fetch_youtube_transcript`) adds the budget of the calling turn's
channel, found from its `origin_context_id` (or the key's own channel). A
routine such a key saves without a destination records that channel. Each scheduled
routine fire runs all three, the budget last against the routine's channel
(after the agent pin check), and records the reason as that run's error
instead of raising. Wakes pass through
chat admission, so all three apply. Unprompted thread participation (Discord and Teams)
checks all three too, and skips silently, on the grounds that a billing
notice is owed to someone who actually asked.

Two boundaries of the design worth stating plainly:

- **The gates run once, before the turn; debits land per model call.** Nothing
  re-checks mid-turn, so a single long turn can take a tenant's balance
  negative. The ledger allows it. Concurrent turns compound this: every turn
  admitted while the balance was still positive runs to completion, so a chat
  tenant can overdraw by up to its effective concurrent-turn cap (the adapter's
  `max_concurrent_turns_per_tenant`, default 3, or the tenant's `turn-cap`
  override) times one turn's cost. Discord's optional process-wide limit refuses
  excess guild mentions and DMs before `admit()`. An MCP `start_turn` session is
  worse. Its spend
  reaches the ledger only when the scheduler's usage sweep next runs (after
  the tick's routine fires, which can take up to the 45-minute turn ceiling).
  Until then the gate reads a balance that leaves out earlier headless turns,
  and MCP turns have no concurrency cap. The overdraft is therefore bounded
  by what a tenant can start between two sweeps, not by N concurrent turns.
  `formal/metering/BalanceGate.tla` checks the concurrent-turn bound for chat
  turns, where it holds, and has a counterexample in which headless turns
  exceed it. The between-sweeps bound is stated here, not model-checked.
  Neither bound covers a refund or dispute of credit already spent: that
  clawback is a further debit, and can take the balance lower still.
- **`BillingExempt` usage is absorbed by the operator, not debited to the
  tenant.** A caller with no platform user identity (an operator, CLI or
  internal token) runs with no balance check, no cap check, and no usage row
  or debit. That is deliberate, and `_admit` asks in as many words that no one
  add a fallback that bills it. The same holds for a headless run with no
  recorder (`BillingExempt(reason="headless-unrecorded")`). The live recorder
  never sees these turns, and the session such a caller creates is stamped
  `daimon_billing_exempt=<reason>` (`create_session` in
  `packages/core/daimon/core/sessions.py`), so [the sweep](#the-tables) skips
  it too. The sweep still prices the session and logs what the tenant would
  have paid; see [Seeing absorbed spend](#seeing-absorbed-spend).

  The stamp is written once, when the session is created, so **the posture of
  the session's creator covers every turn on it.** The same account can hold
  both an internal token and a platform token, and `continue_turn` checks only
  the agent and the account, so one session can see both kinds of caller. A
  platform user continuing an exempt session is absorbed as well; an exempt
  caller acting on a billed session (an MCP `continue_turn` with an internal
  token, or `daimon run --session` on a chat thread's session) is debited to
  the tenant by the sweep, with the platform user taken from the session's
  account stamp. `daimon run` normally targets a session from
  `daimon sessions create`, which carries no tenant stamp and is never swept.

## Channel budgets

An admin can cap what one channel may spend with `set_channel_budget` (MCP)
or `daimon channels budget set PLATFORM WORKSPACE_ID CHANNEL_ID USD`. A
budget is one row in `channel_budgets` per `(tenant, platform, channel)`;
with no row there is no limit, and nothing is created by default. It works
the same with or without Stripe and for either funding mode. Budgets exist on
Discord, Slack and Teams channels; a Teams 1:1 chat has none.

- **Spend** is the channel's debits in `tenant_ledger` inside the window,
  markup included, whatever paid for them (promo credit too): what the
  tenant was charged for turns there, not the pre-markup usage `/billing`
  totals. Debits carry the parent channel, so a thread counts toward its
  channel. A session records it in its own
  `daimon_budget_channel` metadata stamp, which [the sweep](#the-tables)
  reads; it is kept apart from `daimon_channel`, where the conversation runs,
  so a DM counts toward its source channel without being placed in it.
- **Window**: `monthly` (the UTC calendar month), `total` (since `starts_at`,
  or ever) or `fixed` (from `starts_at` until `ends_at`, exclusive). A
  budget with a `starts_at` gates nothing before it, and a fixed one nothing
  after its end. Panels show a fixed window as `START until END`, and a
  total window that has not started as `from START`.
- **The gate** trips once spend reaches the limit, so a limit of 0 stops the
  channel. Like the other gates it runs once before a turn, so a turn in
  progress finishes past the limit.
- **The notice.** The first chat turn the gate refuses in a window DMs the
  channel's admins (users granted directly, and members whose roles at
  their last turn match a granted role), or the server admins when the
  channel has none, at most ten people, on Discord, Slack and Teams
  (`daimon.core.channel_budget_notice`). A monthly budget's window is the
  month; any other budget's is the budget itself. Setting or raising the
  budget re-arms it (`channel_budgets.exhausted_notice_key`), and so does a
  notice no admin received, so a later refusal tries again. It follows the
  tenant's DM policy, never changes the refusal, and is off for a tenant set
  to `false` in `DAIMON_BUDGET_NOTICES`. Refusals of MCP turns, media calls
  and routines send none.

Members can read a channel's budget with `get_channel_budget`; listing,
setting and clearing are for server admins only, never a channel's own admins
(`daimon.core.authz`, `SET_CHANNEL_BUDGET`); each change is recorded in `security_audit_events`, from the CLI too. `/billing` in a channel with a budget
shows `this channel: $spent of $limit (window)`. An admin's `/billing` also
lists the five most used budgets on that platform (active ones first, by
share of the limit spent) with a count of the rest.

What a budget does not cover:

- **Earlier spend.** Attribution starts with this release; debits recorded
  before it have no channel and count toward no budget.
- **DMs started before this release** have no source channel, so their
  turns are neither attributed nor gated. A DM moved with `/dm` since then
  records the parent channel it came from
  (`direct_message_conversations.source_channel_id`): `/dm` is refused while
  that channel's budget is used up, and the DM's turns and media calls
  count toward it and are gated by it. `get_channel_budget` in such a DM
  reads that channel's budget.
- **MCP turns from an unbound key.** An agent key minted with "Use from your
  coding tools" in a sealed channel, or in one its agent is pinned to, records
  that channel (`mcp_tokens.channel_id`): its `start_turn`, `ask` and
  `continue_turn` are gated by that channel's budget, and the sessions it
  opens carry `daimon_budget_channel`, so their spend is attributed there.
  Every other key, the hub and bearer tokens record no channel and are not
  gated, except on an isolated channel's own agent: every run of it, an
  admin's or that channel's admin's from the hub or a DM included, is gated
  by and charged to that channel. Its routines can only post there, so they
  already are. A media tool call without a live `origin_context_id` is not
  attributed either.
- **Routines with no channel**: made without a destination outside a
  channel (no `origin_context_id`, or from a DM), and Discord thread
  destinations saved before this release (see
  [routines.md](routines.md#turning-routines-on)).

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

1. `/billing` on Discord or Slack (`billing` on Teams) offers fixed amounts, each labelled with an
   estimated number of turns derived from that tenant's own history. Admin
   status is verified at click time, not at render.
2. The chat adapters never import `stripe`. They mint a token and POST to
   `/billing/checkout` on the MCP server
   (`packages/adapters/mcp/daimon/adapters/mcp/checkout.py`), which takes the
   tenant from the verified token claim and never from the request body.
3. `packages/adapters/mcp/daimon/adapters/mcp/webhooks.py` handles the
   callback. It takes the amount from Stripe's own `amount_total` rather than
   from metadata. The event id dedups `payment_events`; the payment intent
   identifies the ledger credit, so a second completion event for the same
   payment cannot add a second top-up. Credit claim and ledger insert share
   one transaction. Refunds and disputes insert negative entries against a
   cumulative high-water mark per payment intent, so concurrent callbacks
   cannot claw back more than the original credit. A signed refund or dispute
   received before its Checkout credit is stored, then applied in the same
   transaction that creates the credit. The pending event has no tenant id
   until that credit establishes the tenant. Unmatched pending events are
   removed after 90 days by later webhook traffic.

Completion and clawback transactions serialize by payment intent
(`formal/billing/Clawback.tla` and `OutOfOrder.tla` model this ordering). An existing
credit with a different tenant or amount is an integrity conflict: processing
fails and rolls back so the event remains retryable for investigation. A
retry alone cannot resolve inconsistent payment data; inspect the Stripe
event, payment intent, original credit, and tenant before replaying it.

Both the checkout and webhook routes are mounted only when Stripe is
configured. Operators can credit an existing tenant without Stripe:

```sh
daimon tenants credit discord GUILD_ID 25.00 --note "hackathon grant" --id grant-1
```

The ledger reason is `manual_credit`; the readable note goes in the idempotency
key. The command prints the new balance, credit id and key. Reusing the same
arguments and `--id` does not add a second credit. Omit `--id` for a new,
generated id each time.

Set a per-person monthly cap, with an optional user override:

```sh
daimon tenants cap discord GUILD_ID 10.00
daimon tenants cap discord GUILD_ID 5.00 --user PLATFORM_USER_ID
```

The default row applies to everyone without an override. A zero cap blocks
all turns; the cap gate applies even without Stripe.

## Promo codes

An operator can hand out credit as a code that a tenant admin redeems. Nothing
is visible until some code can be redeemed: only then does `/billing` show
admins a **Redeem code** button, and the Discord ready message and the Slack
install page point them at it.

```sh
daimon promo create --amount 20                    # prints a generated code once
daimon promo create --amount 50 --timed \
  --starts 2026-06-01T09:00Z --ends 2026-06-03T18:00Z --max-redemptions 40
daimon promo list
daimon promo redemptions CODE_ID
daimon promo revoke CODE_ID
```

Codes are deployment-wide and stored only as a SHA-256 hash, so a lost code
cannot be shown again. Generated codes are 20 Crockford base32 characters in
dash-separated groups of five; matching ignores case, spaces and dashes, and
reads O as 0 and I or L as 1. `--code` sets a chosen code of at least 12
characters instead. `--amount` is at most $999,999.99. `--redeem-from` and
`--redeem-until` bound when it can be redeemed, and `--max-redemptions` how
many tenants may redeem it. Each tenant redeems a code at most once.

- **Credit** codes add a `promo_credit` ledger entry at once, keyed
  `promo:{code_id}:{tenant_id}`. It is ordinary credit and never expires.
- **Timed** codes grant their amount only between `--starts` and `--ends`.
  Redemption stays open until `--ends` unless `--redeem-until` is earlier. A
  code redeemed before its start is granted by the scheduler when the window
  opens (same key). At the first scheduler tick after the window closes, a
  `promo_expiry` entry, keyed `promo_expiry:{code_id}:{tenant_id}`, removes
  what was not spent, so spend after the window comes from ordinary credit
  only. Spend inside a window draws on timed credit first, the credit that
  ends earliest first, then on ordinary credit. A window that opens and closes
  while the scheduler is down expires without a grant.
- **Channel budget** codes (`--channel-budget`, `kind=channel_budget`) add
  their amount to one channel's [budget](#channel-budgets) limit instead of
  the balance, and write no ledger row. The raise is permanent: on a
  monthly budget it raises every month's limit. They are redeemed in the
  channel they raise (`/billing` there, or `redeem_promo_code` with
  `channel_id`), which must have a budget; only server admins redeem them,
  like every code, and the redemption records the channel
  (`promo_redemptions.channel_id`).
- **Late spend.** Every turn debit stores `occurred_at`, the model call's own
  time, so a call the [sweep](#the-tables) records after the window closed
  still counts as spend inside it. Fifteen minutes after the close a
  `promo_expiry_refund` entry, keyed
  `promo_expiry_refund:{code_id}:{tenant_id}`, credits back that late spend,
  never more than the expiry removed. Until then late spend is debited twice,
  once itself and once inside the expiry, so for up to about fifteen minutes
  the balance can read low or negative, or hit the balance gate, until the
  refund lands. Spend recorded later counts as ordinary spend: after a grant's
  reconcile, spend recorded inside its window is still charged to that grant
  first, as far as it had credit left, even where it overlaps a later grant
  that is still open. That part is paid from ordinary credit and never
  credited twice.

The balance is still `SUM(delta_usd)` and the gates never read promo state:
timed credit only changes what the ledger holds. `/billing` shows live timed
credit and when it ends. Admins redeem from `/billing` on Discord or Slack,
`billing` on Teams, or with the MCP tool `redeem_promo_code`. Refusals are one of
`invalid`, `revoked`, `not_started`, `expired`, `exhausted`,
`already_redeemed`, `throttled`, `needs_channel` (a channel budget code
redeemed outside a channel), `no_channel_budget` and `not_allowed` (the
caller may not redeem that kind of code there); five refusals in 15 minutes pause a
tenant's attempts, which are serialized per tenant so parallel guesses
cannot slip past. Revoking stops new redemptions only: redeemed credit,
including timed credit not yet started, stays.

An integration can issue codes over MCP with an operator token holding
`promo:create` (see [architecture](architecture.md#operator-tokens)):
`create_promo_code`, `list_promo_codes` and `revoke_promo_code` take the same
terms as `daimon promo`, and no server admin sees them. A token minted with
`--max-issued-usd` (whole cents) may issue at most that much credit in total,
counted as `amount_usd × max_redemptions` per code (so `max_redemptions` is
required); `mcp_tokens.issued_usd` tracks the total, and revoking a code gives
none back. No operator token can open a top-up: `/billing/checkout` answers 403.

## The tables

| Table | Holds |
| --- | --- |
| `tenant_ledger` | every credit and debit, append-only. **Balance is `SUM(delta_usd)`, never a column.** A unique `idempotency_key` prevents replayed writes; top-ups also check the payment intent to recognize older event-keyed credits. Debits carry the `channel_id` they count against, if any. |
| `usage_events` | token counts per model call, with the same `channel_id`. No money column; cost is computed on read. |
| `payment_events` | Stripe webhook dedup, keyed by the Stripe event id, with the compare-and-set `credited_at`. Explicitly not a ledger. |
| `pending_payment_clawbacks` | verified refunds and disputes received before the Checkout credit; keyed by Stripe event id and joined to the later credit by payment intent. |
| `tenant_user_caps` | per-person monthly caps, with a null-user row as the tenant default. |
| `promo_codes` | deployment-wide codes, by hash, with their amount, windows and redemption limit. |
| `promo_redemptions` | one row per code and tenant, with when a timed grant was made, expired (`expired_usd`) and reconciled (`reconciled_at`), and the channel a channel budget code raised (`channel_id`). |
| `promo_redeem_failures` | refused redemption attempts per tenant, for the throttle. |
| `channel_budgets` | per-channel spend limits and their windows; no row means no limit. |

These tables are declared in `packages/core/daimon/core/_models.py` with stores
beside them in `packages/core/daimon/core/stores/`. Ledger reasons in use:
`trial`, `topup`, `manual_credit`, `promo_credit`, `promo_expiry`,
`promo_expiry_refund`, `turn_debit`, `checkpoint_debit`, `media_debit`,
`classifier_debit`, `thread_naming_debit`, and the two clawback reasons named
after their Stripe events.

**The sweep.** An MCP `start_turn` creates a session and sends a message but
never drives the stream, so the inline hook never fires for it.
`packages/core/daimon/core/usage_sweep.py` closes that hole: each scheduler
tick it lists Managed Agents sessions, skips any without a `daimon_tenant`
stamp, belonging to a tenant this deployment does not own, or stamped
`daimon_billing_exempt`, and replays the rest's `span.model_request_end`
events through the same recorder. After a successful pass, it skips event
reads for sessions last updated before that pass started minus 15 minutes.
The watermark stays in scheduler memory; startup and hourly passes read all
stamped sessions, and a failed pass leaves the watermark in place. It is safe to run
against already-metered sessions precisely because the idempotency grain is
the same.

The sweep and the live recorder race for each model call, and the first
commit wins. The amount is the same either way, because both price at the
session's own model. The attribution is not: when the sweep commits first, or
is the only writer (a crashed adapter, an interrupted turn, a headless
session), the row carries `turn_debit` and the platform user of the session's
`daimon_account` stamp. It does not carry the turn's reason and author.

The sweep bills every session whose `daimon_tenant` stamp names a tenant in
its own database, and a session carries no deployment identity. Tenant ids are
derived from `(platform, workspace id)`. Two deployments whose API keys share
one Managed Agents workspace, and which both have the same Discord server or
Slack workspace installed, would each debit the other's sessions. Give each
deployment its own Managed Agents workspace.

### Seeing absorbed spend

For every exempt session it skips, the sweep reads the session's
`span.model_request_end` events, prices them exactly as the recorder would,
and logs `usage_sweep.exempt_skipped` with `tenant_id`, `managed_session_id`,
`reason`, `model_id`, `priced` (false for a model with no published rates,
which prices at zero as in the recorder), `model_calls`, the four token
counts, `cost_usd` (the raw price) and `would_be_debit_usd` (with
`DAIMON_BILLING__MARKUP` applied). Each pass ends with one
`usage_sweep.completed` line carrying `recorded`, `exempt_sessions`,
`exempt_model_calls`, `exempt_cost_usd` and `exempt_would_be_debit_usd`.

Nothing is written to the database for these sessions. An exempt session is
logged again when its events are read, including the hourly full pass. Total
the absorbed spend by distinct
`managed_session_id` (taking its latest line), not by summing every line or
the per-pass totals.

## Settings that turn things on

Policy is nested under `DAIMON_BILLING__`; the Stripe secrets are flat and
deliberately kept separate. Both blocks are catalogued in
[configuration.md](configuration.md#billing-policy) and
[configuration.md](configuration.md#billing-stripe).

`load_billing_config` in `packages/core/daimon/core/billing.py` requires
**all** of the flat Stripe variables together. If any one is missing it logs
that Stripe billing is disabled, returns `None`, and these things follow:

- the checkout, webhook and landing routes are not mounted;
- metering, debits, the balance gate, configured monthly caps and the trial
  credit are unaffected.

Self-service top-ups additionally need `DAIMON_MCP__PUBLIC_URL` and
`DAIMON_MCP__JWT_SECRET` for the adapter-to-MCP hop, and the image must carry
the optional `billing` extra, which is what pulls in `stripe`.

Discord and Slack status cards end on a summary of tokens, cost and, for
prepaid tenants, the balance left after the turn's debit. In a channel with an
active budget (a DM: its source channel's) it shows instead what that budget
has left, for either funding mode. The answer replaces
the card (unless a completion ping posts it fresh), so the summary stays only
above a pinged answer or on a turn with none; answers themselves carry no
usage line on any platform. Teams cards show no summary.

## What you can see

`/billing` on Discord and Slack, and `billing` on Teams, is the reporting surface, always over the
current calendar month, built from
`packages/core/daimon/core/stores/usage_events.py`. A member sees their own
spend, turn count and cap plus the tenant balance; an admin additionally sees
tenant totals and a per-member breakdown. Admin-only figures are not fetched
for a non-admin rather than fetched and hidden.

Two things those numbers are not. They are pre-markup, as above. And tenant
aggregates exclude rows with no platform user attached, so spend recovered by
the sweep from a session with no account stamp is debited to the ledger but
does not appear in the panel's tenant total.
Usage the operator absorbs (`BillingExempt` sessions) is in neither the
ledger nor the panel; it appears only in the sweep's logs, as described in
[Seeing absorbed spend](#seeing-absorbed-spend).

For a single finished turn there is `get_turn_cost` in
`packages/adapters/mcp/daimon/adapters/mcp/tools/agent_chat.py`, which folds
that turn's events into a raw pre-markup figure and returns it as a decimal
string — never a float, and `None` rather than `0` for an unpriced model,
because zero would falsely claim the turn was free.

### Turn outcomes

`turn_outcomes` records terminal reasons and timings separately from billing. Its
content-free usage references join `usage_events` on `(managed_session_id,
event_id)`; one outcome may refer to multiple model calls or recovered sessions.
Refused turns have no usage references, and each admission refusal has its own
reason: `admission_balance_depleted`, `admission_cap_exceeded`,
`admission_channel_budget_exceeded`, `admission_channel_protected`,
`admission_agent_pinned_elsewhere`, `admission_channel_isolated` and
`admission_concurrency_shed`. `admission_denied` covers the invoker allowlist,
an unreadable access policy and any other gate; rows written before
`0044_admission_refusal_reasons` record every protection, pin and isolation
refusal under it. The best-effort outcome writer never
changes a balance, cap, price or ledger debit, and a missing diagnostic row does
not mean no model work was billed. See the turn-outcome contract in
[architecture](architecture.md#durable-turn-outcomes).


## Per-turn usage telemetry

`daimon usage turns TENANT_UUID --days 7 --json` lists content-free turn
measurements. Add `--channel CHANNEL_ID` or `--origin routine` to filter;
`--summary` groups by platform, channel and origin; each listed turn's `reason`
is its [outcome reason](#turn-outcomes). The command reads the local
database and makes no upstream API calls. `--limit` bounds the individual-turn
listing (1–1000); summaries cover the whole selected date range.

Each measurement shares its UUID and atomic database row with the terminal
outcome. It includes model-call count, input/output tokens, cache read/write
tokens, model IDs, and estimated provider cost using the existing static pricing
table. Replayed span IDs are counted once per session; recovery attempts are
summed into the same logical turn. Both billed and exempt turns are measured.
`billing_posture` describes the observed billing path (`metered`, `exempt`,
`mixed`, or `none`), not confirmation that a debit committed. The ledger remains
authoritative for charges; telemetry changes no prices, debits, caps or gates.

Unknown or unavailable session models produce null cost and an `unpriced_calls`
count. Historical outcome rows retain null metrics; new refusals with no model
calls record zero. A summary's `cost_usd` is null if any constituent turn has
unknown cost; `known_cost_usd` is the subtotal of fully priced turns, and
`measured_turns` distinguishes measured rows from history. Token totals include
only measured rows. Existing-session CLI runs without model metadata can report
tokens with unknown cost. Separately metered tool models and auxiliary API calls
are not included in these model-span totals. MCP agent-chat and hub
start/continue/ask calls record outcomes, but their SDK polling paths do not
consume model spans: usage fields remain null and summaries do not count them
as measured turns. Their admission refusals still record measured zero usage.
Library headless calls without an outcome session factory are unobserved.

Telemetry inherits the outcome writer's bounded best-effort delivery: database
outages, saturation or abrupt process termination can lose rows. It is operational
measurement, not an accounting reconciliation source. No prompts, answers, tool
arguments, output text or error messages are persisted here.
