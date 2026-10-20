# Billing and credits

Neutral-backend usage revisions can be recorded through the host accounting
outbox in the caller's database transaction. The first revision retains the
`turn:{session}:{event}` debit key; later revisions use
`adjust:{binding}:{observation}:{revision}` for the signed difference between
rounded totals. A failed transaction rolls back the claim, usage projection and
ledger write together, allowing pending revisions to be replayed after restart.
Corrections retain the original observation's billing time and contribute to
channel spend, including refunds. Historical turn credits keep their existing
channel-spend treatment.

Callers select one billing grain: model requests by default, or turn/session
observations for a provider that reports those units. Incremental and cumulative
observations both use their per-ID revision totals. An aggregate's explicit
coverage must reference durable observations at the selected grain before it
can be acknowledged without another debit; unresolved coverage or an uncovered
mismatched grain fails before claiming the outbox row. Unknown token
stages remain unknown there; known disjoint stages can be priced, and a legacy
usage row is projected only once all four stages are known. Callers retain the
same tenant, user, channel, rates, markup and billing grain across revisions.
The established Anthropic live recorder continues to use its existing transaction
and keys.

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
driver at all; [settling the session](#settling-a-session) bills them when
that turn ends.

### Settling a session

`settle_session` in `packages/core/daimon/core/usage_sweep.py` is the one
path that bills model calls no live stream metered. It lists the session's
`span.model_request_end` events newest first, skips those with a
`usage_events` row (written in the same transaction as the debit, and looked
up by event id), and replays the rest through the recorder. Price, markup, budget channel, the
`turn_debit` reason and the `turn:<session>:<event>` key are the live
path's, so a call recorded twice is debited once.

- **Chat turns.** When a chat turn is over (after its answer, failure or
  ceiling message is shown), `run_prepared_turn` settles the session with
  the turn's own recorder: at most 10 pages of 100 calls in one 5-second
  budget, outside the turn ceiling. This bills calls made while the adapter
  was down (a deploy or crash mid-turn) or after an interrupt or ceiling.
- **Headless agent-chat turns.** MCP `start_turn`, `continue_turn` and `ask`
  never drive a stream. Each send first commits a settle request (inside
  `continue_turn`'s session fence, so requests follow the order of sends),
  then sends, then names the accepted `user.message` on the request. If the
  request cannot be written nothing is sent. A send Managed Agents rejects (a
  4xx) withdraws its request and restores the one it replaced; after an error
  that may hide an accepted send (a timeout, a 5xx) the request stays. When
  `get_my_session`, `list_events` or `ask`'s own wait next reads the session
  `terminated`, or `idle` with the turn's own message listed and a
  `session.status_idle` or `session.status_terminated` event listed after it
  (a bare `idle` right after a send is the previous turn's, and a message not
  listed yet means the turn has not ended), the read settles the session
  before it returns and clears the request once the settle has read to the
  end, and only if it is still the very request that read verified (a newer
  send's request is never consumed). A request not yet named by its send is
  left alone for ten minutes, while that send may still be in flight; after
  that, an unnamed request (its naming write failed) waits on the first
  message processed after it was written. The whole step, database work
  included, is bounded by 5 seconds and 10 pages; out of budget, the request
  stays. The recorder is
  built from the session's stamps (below). A billing error is logged as
  `billing.settle_failed` and never fails the read.
- **No stored progress.** A settle is complete only when it reads the
  listing to the end within its bounds. One cut short keeps every debit it
  wrote, and the next settle reads the listing again, which is safe because
  a call with a usage row is never recorded twice. A headless request stays
  until a complete settle; the drain reads up to 100 pages (10,000 calls) a
  session and logs `usage_sweep.session_incomplete` for a larger one. A chat
  session with more than 1,000 calls since the backlog cutoff has only its
  newest 1,000 read at each turn end. No automatic settle reads a call made
  before the backlog cutoff.
- **Attribution.** For sessions billed by their stamps, `daimon_tenant` is
  the billed tenant (it must exist in this database), `daimon_account`
  resolves to the owning person's platform user, `daimon_budget_channel` is
  the channel charged, and the model is the one frozen in the session. A
  call the chat path recovers carries the platform user of the turn that
  settled it, which is not necessarily the person whose turn was cut off.
- **Evidence.** A settle that finds an unbilled call, or stops before the
  end, logs `billing.settled` with `managed_session_id`, `scanned`,
  `unbilled`, `debited`, `debit_usd` (rows this settle inserted) and
  `complete`.
- **What it does not reach.** A headless session nobody reads again after
  its turn ends is billed by [the sweep](#the-tables). Calls from before
  this path was deployed are [the backlog](#the-backlog).

In a shared thread (`thread_mode="shared"`, see `docs/mux.md`) everyone's
turns run on one session, and the recorder still bills each turn to the
person who wrote it. The sweep cannot: a shared session carries no caller
account, so usage it alone recovers is debited to the tenant without a
person.

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

A lookup for a dated snapshot id such as `claude-haiku-4-5-20251001` (Managed
Agents echoes these) falls back to its alias row (`claude-haiku-4-5`) when the
id itself has no row: only an eight-digit `-YYYYMMDD` suffix is stripped, an
id whose alias is not priced still prices at `None`, and agent selection still
reads `AGENT_MODEL_PRICING` keys only, so snapshot ids do not become
selectable. Before this fallback, turns on a snapshot id recorded no cost and
debited nothing; usage reports and cap reads now price those saved rows, but
the zero ledger debits and null turn outcomes already written are not repaired.

Opus 5.5 is priced at $4 input, $20 output, $5 five-minute cache write, and
$0.20 cache read per million tokens; Sonnet 5.5 at $2, $10, $2.50 and $0.10.
Haiku 5.5 is priced by prompt length: $0.10, $0.50, $0.125 and $0.01 for a
request whose prompt is up to 100,000 tokens, and $0.50, $2.50, $0.625 and
$0.05 above that. The prompt counts input, cache reads and cache writes, and
each request is priced on its own (`ModelRates.long_context`,
`LONG_CONTEXT_PROMPT_TOKENS`). Turn cards also split the turn's token totals by
request prompt length before pricing, so a turn spanning both tiers displays
the sum of its request costs.

`claude-opus-5-5-fast` is a client-side preset (`daimon.core.model_presets`),
never a model id the Managed Agents API sees: `AgentSpec` expands it to
`{"id": "claude-opus-5-5", "speed": "fast"}` on validation, preserving any
authored `effort`/`inference_geo`. It is priced at 2x plain Opus 5.5 ($8
input, $40 output, $10 five-minute cache write, $0.40 cache read) to cover
Managed Agents' fast-mode surcharge. Every place a model id reaches billing or
a cost preview must read the agent's resolved `speed` back out and price
`claude-opus-5-5-fast` instead of `claude-opus-5-5` — `SessionSnapshot.pricing_id`
and `daimon.core.model_presets.model_choice_id` are the two helpers that do
this; a call site that reads a bare `model_id` off a snapshot or native model
silently bills the preset at half its real rate.
`AGENT_PRICING_CHECKED_ON` records the day every row was last checked against
`AGENT_PRICING_SOURCE`. `test_pricing.py` pins each row, so a price edit fails
until the test table and the date move with it, and
`uv run pytest -m contract packages/core/tests/test_pricing.py` compares the
rows with the live pricing page.
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
places. It is applied to the ledger and to the finished-turn summary line,
whose `used` is the turn's debit (its whole-turn cost times `markup`, so it can
differ from the sum of the per-call debits by at most $0.001). Every other
reporting surface reprices raw `usage_events` rows, so what a panel or
`get_turn_cost` shows is provider cost, not what the tenant was charged.

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
[channel budgets](#channel-budgets). A DM moved with `/dm-me` counts toward the
channel it came from. A turn with no channel (an older DM, an MCP turn from
a key not minted in a channel) and a channel with no budget are never gated.
On Discord, Slack and Teams, a finished turn ends with one summary line: the agent,
the turn's time, what it was debited (its model cost times `markup`) and the
money left, for example
`Ada  12s  $0.042 used  $41.20 left`, on the answer's last message: the last
chunk of a long answer. On Discord the turn's files are attached to that same
message; on Teams the line sits in a card below the answer beside 👍/👎 (or on a
tool-only, cancelled or failed turn's card). Em spaces set
the fields apart. The
money left is the active channel budget's remainder; a channel without an
active budget shows the tenant's prepaid balance when one exists. An unpriced
model drops the cost, and an operator-funded tenant outside a budgeted channel
drops the money left.

A denial raises `AdmissionDenied` carrying only the reason literal
(`balance_depleted`, `cap_exceeded` or `channel_budget_exceeded`); the wording
comes from `packages/core/daimon/core/turn/notices.py` with each adapter's
command names. A depleted balance reads "Your team's Daimon credit has run
out." then "An admin can top up with `/billing`." (on Teams, "`billing` in a
1:1 chat with me"). The turn aborts — it never silently degrades to a
cheaper model. The balance and cap checks are re-run, with the same order, by
the MCP tools that start a turn (`_admit` in
`packages/adapters/mcp/daimon/adapters/mcp/tools/_ctx.py`), followed by the
budget of the channel an agent key was minted in; the billed media
tool (`fetch_youtube_transcript`) adds the budget of the calling turn's
channel, found from its `origin_context_id` (or the key's own channel). A
routine such a key saves without a destination records that channel. Each scheduled
routine fire runs all three, the budget last against the routine's channel
(after the agent rule check), and records the reason as that run's error
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
  override) times one turn's cost. A turn over that cap waits in the turn
  queue after `admit()`; it runs the balance gate again once it holds a slot,
  so queued turns do not widen the bound. Follow-up turns read the tenant's
  current turn cap before taking a new queue ticket. Once the adapter observes
  a lower cap, running turns finish, but new turns wait until enough slots are
  free. An MCP `start_turn` session is worse. Its spend reaches the ledger
  only when a read sees its turn ended or the sweep settles it, and MCP
  turns have no concurrency cap. Until then the gate
  reads a balance that leaves out headless turns not yet settled, so the
  overdraft is bounded by what a tenant runs before those reads, not by N
  concurrent turns. `formal/metering/BalanceGate.tla` checks the
  concurrent-turn bound for chat turns, where it holds, and has a
  counterexample in which headless turns exceed it.
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
  `packages/core/daimon/core/sessions.py`), so settling it debits nothing. A
  settle still prices it and logs what the tenant would have paid; see
  [Seeing absorbed spend](#seeing-absorbed-spend).

  The stamp is written once, when the session is created, so **the posture of
  the session's creator covers every turn on it.** The same account can hold
  both an internal token and a platform token, and `continue_turn` checks only
  the agent and the account, so one session can see both kinds of caller. A
  platform user continuing an exempt session is absorbed as well; an exempt
  caller acting on a billed session (an MCP `continue_turn` with an internal
  token, or `daimon run --session` on a chat thread's session) is debited to
  the tenant when the session is settled, with the platform user taken from
  the session's account stamp. `daimon run` normally targets a session from
  `daimon sessions create`, which carries no tenant stamp and is never settled.

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
  `daimon_budget_channel` metadata stamp, which a
  [settle](#settling-a-session) reads; it is kept apart from `daimon_channel`, where the conversation runs,
  so a DM counts toward its source channel without being placed in it.
  If the channel charged by policy changes between turns, Daimon replaces the
  session before the next turn. A running old session must finish first. This
  keeps the session stamp a settle reads aligned with that turn's admission even when
  its live usage event was missed. The immutable stamp is also kept in
  `thread_sessions.budget_channel_id`; older mappings read their stamp from MA
  on the next bind and replace only if it differs. If MA cannot be read, the
  mapping stays unknown and the read is retried on a later bind. A replacement
  checkpoint runs on the old session, so its live debit and any recovered
  debit both count against the old session's stamped channel. The new turn's
  admission gates still apply before replacement; the checkpoint does not
  independently gate the old channel's balance or budget.
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
  their last turn match a granted role, a Slack user group or Teams team
  only if a live lookup still lists them), or the server admins when the
  channel has none, at most ten people, on Discord, Slack and Teams
  (`daimon.core.channel_budget_notice`). It reads "#team-020 has used its
  $5.00 monthly budget." then "Raise the budget to resume now, or wait until
  next month."; a budget that never resets drops "monthly" and the wait. A
  monthly budget's window is the month; any other budget's is the budget
  itself. Setting or raising the
  budget re-arms it (`channel_budgets.exhausted_notice_key`), and so does a
  notice no admin received, so a later refusal tries again. It is sent in
  the background, follows the tenant's DM policy, never changes or delays
  the refusal, and is off for a tenant set to `false` in
  `DAIMON_BUDGET_NOTICES`. Refusals of MCP turns, media calls
  and routines send none.

Members can read a channel's budget with `get_channel_budget`; listing,
setting and clearing are for server admins only, never a channel's own admins
(`daimon.core.authz`, `SET_CHANNEL_BUDGET`); each change is recorded in `security_audit_events`, from the CLI too. `/billing` in a channel with a budget
(`billing` typed in one on Teams, answered in the 1:1 chat)
shows it in a **This channel** section under the credit, worded from its
window (`$1.20 of $5.00 used this month`, `… used since 2026-07-01`, `$5.00
budget from 2026-07-01` before it starts). An admin's `/billing` also lists,
under **Channel budgets** after the top spenders, the five most used budgets
on that platform (active ones first, by share of the limit spent) with a count
of the rest.

Owned offline tests can use `daimon.testing.channel_accounting.ChannelAccounting`
to run a prepared host turn with explicit mock prices through the real usage
recorders, then independently query its channel debits and current budget state
in a read-only transaction. The test seam preserves the configured budget limit;
it does not seed synthetic debits or infer a charge from a reply. Mock prices
prove billing arithmetic, and do not establish provider actual spend or certify
the next turn's admission/refusal. See the testing package README for the API.

What a budget does not cover:

- **Earlier spend.** Attribution starts with this release; debits recorded
  before it have no channel and count toward no budget.
- **DMs started before this release** have no source channel, so their
  turns are neither attributed nor gated. A DM moved with `/dm-me` since then
  records the parent channel it came from
  (`direct_message_conversations.source_channel_id`): `/dm-me` is refused while
  that channel's budget is used up, and the DM's turns and media calls
  count toward it and are gated by it. `get_channel_budget` in such a DM
  reads that channel's budget.
- **MCP turns from an unbound key.** An agent key minted with "Use from your
  coding tools" in a channel with limited readers, or in one its agent's rule
  names, records
  that channel (`mcp_tokens.channel_id`): its `start_turn`, `ask` and
  `continue_turn` are gated by that channel's budget, and the sessions it
  opens carry `daimon_budget_channel`, so their spend is attributed there.
  Every other key, the hub and bearer tokens record no channel and are not
  gated, except on a channel's own agent: every run of it, an
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

An operator can hand out credit as a code. A **server-wide** code is redeemed
by a server or workspace admin and credits the whole tenant. A **channel**
code (`--channel-cap`) can be redeemed by any member and credits the channel
they redeem it in. Nothing is visible until some code can be redeemed: only
then does `/billing` show a **Redeem code** button, to admins for any code
and to members for a channel code, and the Discord ready message and the
Slack install page point people at it.

```sh
daimon promo create --amount 20                    # prints a generated code once
daimon promo create --amount 50 --timed \
  --starts 2026-06-01T09:00Z --ends 2026-06-03T18:00Z --max-redemptions 40
daimon promo create --amount 10 --channel-cap 30 --max-redemptions 100  # channel code
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
many times it may be redeemed in all. Each tenant redeems a server-wide code
at most once.

- **Credit** codes add a `promo_credit` ledger entry at once, keyed
  `promo:{code_id}:{tenant_id}`. It is ordinary credit and never expires.
- **Channel** codes are credit codes with `--channel-cap`, which must be at
  least `--amount`; a timed code cannot have one. Anyone can redeem one from
  `/billing` in a channel (a thread counts as its parent channel), once per
  person per channel, until that channel has taken `--channel-cap` from the
  code: with `--amount 10 --channel-cap 30`, three people in a channel can
  each add $10. Each redemption writes a `promo_credit` entry with the
  channel's `channel_id`, keyed `promo:{code_id}:{tenant_id}:{channel_id}:{account_id}`,
  and raises the channel's [budget](#channel-budgets) limit by the amount, so
  the channel can spend that much more and the credit pays for it. A channel
  with no budget has no limit to raise: the credit adds to the tenant's
  balance, which that channel already spends from. Budgets only count
  debits, so the credit entry never lowers a channel's spend. A channel code
  cannot be redeemed in a DM, a Slack group DM or a Teams 1:1 chat, or with
  the MCP tool. On Slack the conversation's type comes from
  `conversations.info`, not its id (a group DM can have a `G` id like a
  private channel), and a conversation Slack won't describe as a channel is
  refused for a channel code.
- **Rolling out channel codes.** Migration `0088_channel_promo_codes` only
  adds: a nullable `promo_codes.channel_cap_usd`, the
  `promo_channel_redemptions` table and a nullable
  `promo_redeem_failures.account_id`, so workers of the previous release keep
  redeeming server-wide codes during the rollout and after an
  application-only rollback. Those workers don't know channel codes and would
  credit one server-wide, so create channel codes only once every worker
  runs this release.
- **Timed** codes grant their amount only between `--starts` and `--ends`.
  Redemption stays open until `--ends` unless `--redeem-until` is earlier. A
  code redeemed before its start is granted by the scheduler when the window
  opens (same key). At the first scheduler tick after the window closes, a
  `promo_expiry` entry, keyed `promo_expiry:{code_id}:{tenant_id}`, removes
  what was not spent, so spend after the window comes from ordinary credit
  only. Spend inside a window draws on timed credit first, the credit that
  ends earliest first, then on ordinary credit. A window that opens and closes
  while the scheduler is down expires without a grant.
- **Late spend.** Every turn debit stores `occurred_at`, the model call's own
  time, so a call a [settle](#settling-a-session) records after the window closed
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
timed credit only changes what the ledger holds. `/billing` shows the balance
as `$62.40` `total credit left`, a total that already includes live timed credit,
and under it `Includes $25.00 that expires. It's used first.` (everything left in
timed credit). Below zero (an operator-funded tenant) it reads `No credit left`
and `$3.10 spent beyond it`, still with that line. **Expiry dates** lists each
timed credit with its end date, soonest first (five, then a count of the rest). Codes are redeemed from `/billing` on Discord or Slack,
`billing` on Teams, or, server-wide codes only, with the admin-only MCP tool
`redeem_promo_code`. Admin rights are read again when the code is submitted.
Refusals are one of `invalid`, `revoked`, `not_started`, `expired`,
`exhausted`, `already_redeemed`, `throttled`, `admin_only` (a member tried a
server-wide code), `needs_channel` (a channel code outside a channel),
`already_redeemed_in_channel` and `channel_cap_reached`, each shown as one
plain sentence. Every attempt, allowed or refused, writes a
`panel:promo_redeem` security audit row with the channel it was made in.
Five refusals in 15 minutes pause that person's attempts, and only theirs:
another member's, or an admin's, are unaffected. One person's attempts are
serialized so parallel guesses cannot slip past. Revoking stops new redemptions only: redeemed credit,
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
| `tenant_ledger` | every credit and debit, append-only. **Balance is `SUM(delta_usd)`, never a column.** A unique `idempotency_key` prevents replayed writes; top-ups also check the payment intent to recognize older event-keyed credits. Debits carry the `channel_id` they count against, if any, and a channel code's credit the channel it was redeemed in. |
| `usage_events` | token counts per model call, with the same `channel_id`. No money column; cost is computed on read. |
| `usage_reconcile_progress` | per Managed Agents session, a pending headless [settle](#settling-a-session) request (`settle_requested_at`, `settle_turn_event_id`); the reserved `backlog-cutoff` row's `covered_through` records when the settle path was deployed. Its other columns, from an earlier catch-up design, are unused and will be dropped. |
| `payment_events` | Stripe webhook dedup, keyed by the Stripe event id, with the compare-and-set `credited_at`. Explicitly not a ledger. |
| `pending_payment_clawbacks` | verified refunds and disputes received before the Checkout credit; keyed by Stripe event id and joined to the later credit by payment intent. |
| `tenant_user_caps` | per-person monthly caps, with a null-user row as the tenant default. |
| `promo_codes` | deployment-wide codes, by hash, with their amount, windows, redemption limit and, for a channel code, `channel_cap_usd`. |
| `promo_redemptions` | one row per server-wide code and tenant, with when a timed grant was made, expired (`expired_usd`) and reconciled (`reconciled_at`). |
| `promo_channel_redemptions` | one row per channel code, channel and person, for the per-person rule and the channel cap. |
| `promo_redeem_failures` | refused redemption attempts per person (`account_id`), for the throttle. |
| `channel_budgets` | per-channel spend limits and their windows; no row means no limit. |
| `platform_user_names`, `platform_channel_names` | the last name each platform sent for a person (display name and handle) or a channel, so `/billing` can name them when the platform does not answer; see [What you can see](#what-you-can-see). |

These tables are declared in `packages/core/daimon/core/_models.py` with stores
beside them in `packages/core/daimon/core/stores/`. Ledger reasons in use:
`trial`, `topup`, `manual_credit`, `promo_credit`, `promo_expiry`,
`promo_expiry_refund`, `turn_debit`, `checkpoint_debit`, `media_debit`,
`classifier_debit`, `thread_naming_debit`, and the two clawback reasons named
after their Stripe events.

**The sweep.** The sweep is on by default
(`DAIMON_SCHEDULER__USAGE_SWEEP_ENABLED=false` turns it off). It runs on its
own scheduler loop and settles the pending headless settle requests no read
cleared, 50 a pass, least recently visited first: a session whose turn has
ended is settled and its request cleared; a deleted one has it cleared; one
still running, or one whose read fails, keeps its request and goes to the
back of the queue, so it never holds up the others. Any rate limit, on a
session read or an events listing, ends the pass. It reads only the sessions
those requests name, never the whole workspace, and never calls before the
backlog cutoff. A pass costs one session read per pending request (up to 50,
so at most about 6,000 reads an hour at the default 30-second interval with
50 sessions still running), plus the listings of the turns that ended. While
it is off, such a session is billed the next time a read sees its last turn
ended. Each pass logs `usage_sweep.completed` with `pending` and `recorded`.

A settle bills every session whose `daimon_tenant` stamp names a tenant in
its own database, and a session carries no deployment identity. Tenant ids are
derived from `(platform, workspace id)`. Two deployments whose API keys share
one Managed Agents workspace, and which both have the same Discord server or
Slack workspace installed, could each debit the other's sessions. Give each
deployment its own Managed Agents workspace.

### The backlog

Calls made before this settle path was deployed are the backlog. The
migration that introduced it recorded that moment as the `covered_through`
of the reserved `usage_reconcile_progress` row `backlog-cutoff`, and no
automatic settle (chat turn end, MCP read, sweep) reads a call older than
it, so new turns are billed for every tenant and the backlog for none.

`daimon usage settle-backlog --internal UUID [--internal UUID ...]` settles
the backlog of every MCP agent-chat session a turn outcome names (or only
those given with `--session`), each from the start of its history up to the
cutoff, so applying writes exactly the calls the dry run counted; later
calls are ordinary billing. An internal tenant's unbilled calls are debited;
every other tenant's are waived: no automatic settle ever reads them, and
`billing.backlog_waived` logs the tenant's would-be debit, but nothing is
charged. A re-run reports the waived calls again and still charges nothing. Exempt sessions are skipped. It is a dry run unless `--apply` is
given. It prints a receipt: the cutoff, the mode (`dry_run` or `apply`), and
per tenant `internal` or `external`, the sessions and calls it covers, their
raw cost, the would-be debit, `debited_usd` (an internal tenant, applied: the
ledger rows written) and `waived_usd` (an external tenant). With `--json` the
receipt is one JSON object; the command writes no files, so keep the output
of the dry run and of the applied run as the record.
Recording is idempotent, so applying twice debits once.

### Seeing absorbed spend

When an exempt headless session is settled, its calls are read and priced
exactly as the recorder would, and `usage_sweep.exempt_skipped` is logged with
`tenant_id`, `managed_session_id`, `reason`, `model_id`, `priced` (false for a
model with no published rates, which prices at zero as in the recorder),
`model_calls`, `cost_usd` (the raw price) and `would_be_debit_usd` (with
`DAIMON_BILLING__MARKUP` applied). Nothing is written for these sessions, so
each line covers the session's history since the backlog cutoff: total the absorbed spend by distinct `managed_session_id`
(taking its latest line).

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

The summary line's money left is the prepaid balance after the turn's debit
or, in a channel with an active budget (a DM: its source channel's), what that
budget has left, for either funding mode.

## What you can see

`/billing` on Discord and Slack, and `billing` on Teams, is the reporting surface, always over the
current calendar month, built from
`packages/core/daimon/core/stores/usage_events.py`, in the same words on every
platform. A member sees their own use against their cap and the credit left,
with "Ask an admin to add credit."; an admin sees the month's spend and how
many people spent it, the credit left, the **Top spenders** (five, then a
count) and the **Channel budgets**, with the actions **Add credit** and
**Redeem code** (while a code is redeemable) and a **Look up a person**
picker that re-checks admin and shows one person's spend this month (on Teams,
the people picker over the organisation's directory). Both get **Expiry dates** while the credit includes timed credit:
a private reply on Discord, a pushed view on Slack, an expanding section on
Teams. Admin-only figures are not fetched for a non-admin rather than fetched
and hidden. On Discord the panel's accent is red with no credit left or the
viewer over their cap, and amber while timed credit expires within a week.

The top spenders are shown by name, never as a ping and never as an id. Each
shown person is named in this order, all lookups side by side under one short
timeout:

1. **The platform's name for them now.** Discord takes the member cache, else
   one member fetch (no privileged intent), within 1.5 seconds. Slack calls
   `users.info` (display name, else real name) within 2 seconds and shows it as
   escaped plain text. Teams looks the person up on the rosters of the teams
   the bot is installed in, within 2 seconds.
2. **The name stored when we last saw them.** Whenever an adapter already holds
   a name it is kept in `platform_user_names`, one row per (tenant, platform,
   user), for anyone with an account in that tenant
   (`packages/core/daimon/core/platform_names.py`). The name joins a bounded
   in-process queue (5,000 people; a later name replaces a queued one, and
   past the cap new ones are dropped and logged) that one background task
   writes 50 rows at a time, so it never delays or fails a turn and a burst
   of new people holds one database connection. The names come from the
   author of a Discord
   mention and the user of any Discord interaction (server display name and
   username), the `user_profile` on a Slack message and the username on a
   Slack slash command or click, the sender of every Teams message and card
   click, and every name a panel lookup returns. Someone who left the server,
   is in no installed team, or whose lookup failed or timed out gets this one.
3. **Their account's own name**, on Discord only, for someone with no stored
   name: `fetch_user` answers for people who left (global name, else
   username). Slack's `users.info` already answers for deactivated people.
4. **Never named to us at all**: Discord writes a `<@id>` mention, which the
   client shows as their name and which pings nobody (the panel is ephemeral
   and sent with no allowed mentions); Slack writes a `<@U…>` mention, which a
   modal shows as the name (the panel is only ever a modal); Teams says
   `Name unavailable`, since an `<at>` mention there would notify them.

Names are escaped: markdown and mentions on Discord, `&`, `<` and `>` on Slack,
markdown and tags on Teams. Discord and Slack show channel budgets as channel
mentions, which the client names. Teams shows each channel's name: from the
channel listings of the installed teams, else the name stored from the last
message there (`platform_channel_names`), else `General` for a team's own
channel, else `Channel name unavailable`; never the `19:…` id.

The panel has no `·` separators: the admin subtitle is two lines (`October
2026`, `$48.17 spent by 9 people`), an expiry date reads `$20.00 on Oct 12`,
and a top-up amount is `$10` with `about 100 answers` under it.

A privacy deletion removes the person's stored name with the rest of their
records, in every tenant their account has a principal in, and drops any name
of theirs still queued. A name is only written while the person has a
principal there, and the deletion removes it after the principal, so a write
racing the deletion, from any process, cannot bring it back; someone with no
account has no stored name, so `/privacy` saying "no data on file" stays true.
Deleting a tenant removes its stored names.

Two things those numbers are not. They are pre-markup, as above. And tenant
aggregates exclude rows with no platform user attached, so spend recovered by
a settle from a session with no account stamp is debited to the ledger but
does not appear in the panel's tenant total.
Usage the operator absorbs (`BillingExempt` sessions) is in neither the
ledger nor the panel; it appears only in the settle logs, as described in
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
`admission_channel_budget_exceeded`, `admission_channel_protected` (writers
`none`), `admission_agent_pinned_elsewhere` (its agent rule names other
channels), `admission_channel_isolated` (readers `own`, and the agent isn't one
of the channel's own) and `admission_concurrency_shed`; the stored names
predate channel and agent rules. `admission_denied` covers the invoker allowlist,
an unreadable access policy and any other gate; rows written before
`0045_admission_refusal_reasons` record every channel and agent rule refusal
under it. The best-effort outcome writer never
changes a balance, cap, price or ledger debit, and a missing diagnostic row does
not mean no model work was billed. See the turn-outcome contract in
[architecture](architecture.md#durable-turn-outcomes).
For a session replacement, the checkpoint's outcome sample is priced at the
predecessor session's model. Its `checkpoint_debit` counts against that
session's budget channel; the successor's calls use the admitted channel.
An outcome's `unpriced_calls` describes diagnostic price coverage, not whether
the live recorder inserted a debit. Check `usage_events` and `tenant_ledger`
by `(managed_session_id, event_id)` to establish the money consequence.


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

## Usage measurements

Alternate-provider observation IDs have a fixed coverage set: revisions may
correct amounts but cannot add or remove covered IDs. Host ingest refuses
overlapping sibling aggregates, including shared transitive or absent descendants.
Pending observations participate in this check; invalid coverage cannot claim an
outbox row. Dynamic coverage reconciliation is unsupported.

Accounting accepts provider-neutral usage observations. Input counts include
cache reads and writes; billing projects them back into the existing disjoint
token stages before pricing. Unknown counts remain unknown. Anthropic model
events retain their native event IDs, immutable revision 1 and provider
timestamps, so replaying historical events leaves usage and ledger rows unchanged.

Usage backfill reads billed and exempt model-request spans through the neutral
usage port, retaining the ascending model-request filter and native paginator.
Its complete session inventory uses an explicitly authorized workspace scope
and preserves native pagination and session snapshots. The host validates
tenant metadata before span reads and preserves historical
event IDs, debit keys, timestamps, markup and absorbed-spend log totals.

Explicit alternate backends retain native usage observations at their selected
interaction, turn or session grain. Cumulative corrections replace prior totals;
turn and session aggregates cannot both charge the same work. Settlement uses
verified dated model prices and explicitly measured infrastructure cost, with
signed corrections applied once in the usage and ledger transaction. Missing
usage, unknown model attribution or unverified actual infrastructure leaves the
observation pending for reconciliation. A known inapplicable cache-write bucket
does not erase uncached input spend. Anthropic keeps its existing metering path.

Prepared turns for an explicitly configured alternate provider use its authorized
native session and bound observation recorder. Usage frames can arrive without
a display event; nullable measurements and pending accounting stay intact.
The default Anthropic session continues to use its SDK span recorder.
OpenAI probe reconciliation can read captured Admin Costs and Completions Usage
exports through `python -m daimon.testing.conformance.usage_reconciliation --costs costs.json
--usage usage.json --attestation attestation.json --guard-config budget.json
--spend-ledger spend.md`. This command reads files only and emits an assessment
and, when eligible, an unsigned proposal bound to the current hold. Lead approval
and the existing append-only guard application are separate. No key is discovered
and no charge or hold changes during proposal generation.

The [Admin Costs API](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs)
provides daily buckets, and [Completions Usage](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/completions)
reports grouped token counts. The collector requires an explicitly supplied
[Admin API key](https://developers.openai.com/api/reference/administration/overview)
and transport. Buckets are project totals: reconciliation supports an audited
isolated project window containing exactly one run, across all keys and sources.
It never prorates a shared project's bill. Final billing evidence and inventory
hashes must bind the complete exports; an exhausted page cursor does not prove
final posting. Every page must record the effective organization, origin and
full-scope query, including its pagination cursor. The collector rejects client
query, authentication, cookie, custom-header and event-hook defaults before IO.
Independent final-bill totals and amounts for every classified line item, plus
inventory token/request totals, must match the exports exactly. Copying these
expected totals from the candidate export is not evidence of completeness; the
lead must inspect the separate inventory and final billing artifacts before
approval. An omitted infrastructure charge or any amount/count mismatch refuses
the proposal and retains the hold.
Missing amounts, empty/delayed bills, duplicate groups, foreign
scope, unknown line items or ambiguous attribution remain estimated, unverified.
Exact billed token and container line items replace estimates only after approval.
An `ObservationBilled` callback is an accounting posture, not provider
enablement. Prepared foreign turns require authorized native resources and
profiles, genuine usage frames, bounded authoritative post-run usage fetches,
and the admitted persistence lease/journal. Explicit host registration and
adapter support must supply those prerequisites. Foreign `ProviderUsageTotals`
compatibility counters are lower bounds. Before a foreign profile is enabled,
each adapter footer must omit cost unless verified provider prices and actual
infrastructure are available, and must not price those counters through an
Anthropic meter. Today's Discord, Slack and Teams footers still price the legacy
counters through `MODEL_PRICING`, so foreign enablement is gated on that adapter
work.

An explicit OpenAI native session spend cap is cumulative in integer USD cents;
it does not replace the host budget guard or establish an invoice ceiling.
Native `spend_control.consumed` is nullable and rounded down to whole cents, so
it cannot certify exact spend or zero usage. Actual model usage, dated prices
and container/session charges must still be measured separately. Missing or
changed configured caps refuse before host input, while omitted caps preserve
existing behavior.

OpenAI delegated usage has an offline accounting API in
`daimon.core.usage_delegation`. The admitted host supplies a frozen, bounded
inventory of native turn IDs, subagent IDs and proven ancestry to one root.
Native child observation IDs and meters stay intact; the accounting `turn_id`
is fixed to that root and accounting binding. A child cannot move to another root on replay, restart or
correction. Nested children remain separate work, including later turns from a
reused subagent. Missing batch members, invalid ancestry, overlapping aggregates
and omission of previously recorded work are refused before settlement.

Inventory completeness and disjoint model-work coverage default to unverified.
The host must establish both from authoritative native records before setting
the corresponding scope decisions. A populated tree, exhausted cursor or
subagent creation event alone is not proof. The current OpenAI collector does
not establish that inventory or verified child model context, so this API does
not lift G1's delegation-disabled restriction. Native collection and enablement
remain a separate owner follow-up; no provider parent-turn field is assumed.

Every root and child observation enters one transaction, including null usage.
Unknown disjointness, inventory completeness, child model/tariff, token counts
or actual infrastructure leaves unsatisfied observations durably pending and
the root outcome cost unverified. Children never inherit a parent's tariff.
The explicitly measured shared session/environment and auxiliary-charge total
is charged once on the root; children contribute their independently priced
model work. No missing infrastructure is filled with zero. Same-revision retries
can settle newly verified context; changed settled amounts or newly attributed
models require a higher observation revision. Capture follows successful
commit, so a batch failure rolls back every debit and emits no usage sample.
Gemini Flash currently retains interaction usage; this API refuses to invent
independent child meters or add delegated costs to potentially inclusive totals.


### Explicit offline footer accounting hooks

`daimon.testing.footer_accounting.FooterAccountingHook` joins real
`DiscordSurfaceCapture` posts with a read-only, consistent snapshot of an
authorized binding's persisted usage revisions, accounting outbox and tenant
ledger. Supply independently captured normalized `UsagePriceReceipt` values,
including the native revision, dated controlled price, explicit infrastructure
cost/source, and an authoritative root terminal. The shared owned-sessionmaker
guard checks every bind before connection; plain local `daimon_test` is refused
by design (use an owned lane database, or the registered isolated CI schema).

`evaluate` returns the two D11 checks and the independently read ledger amount.
The actual embed's `used` field is parsed from accepted adapter payloads; balance
values and draft/model text cannot substitute for a footer. Signed correction
debits/credits are joined by exact observation/revision keys. Unknown pricing,
container time, pending outbox, or incomplete native turn coverage remain
PENDING. A complete root turn usage receipt can prove controlled host arithmetic;
request-span subtotals and delegated/covered aggregates cannot certify a complete
turn. OpenAI/Gemini currently omit the display cost, so their footer check remains
PENDING. These hooks never certify live spend, change adapter defaults, create
aggregate rows, or restore the cut C12 aggregate certification. The catalog
executor must separately bind actual capture/receipts; installing a hook does not
promote a source cell.

Offline tests can read normalized native revisions through
`stores.turn_usage.read_accounting_revisions` without claiming or replaying the
accounting outbox. `daimon.testing.turn_accounting.TurnAccountingReader` adds a
coherent read-only snapshot restricted to owned test databases, explicit dated
rate/model and infrastructure evidence, and independent native turn inventory
coverage. Its completeness flag requires complete measured prices and coverage,
settled revisions and an exact ledger/debit match. Unknown rates or container
time remain unknown; a known ledger debit does not establish a complete turn
cost. Controlled test evidence certifies host arithmetic only. The API preserves
historical Anthropic float/debit rounding and does not change displayed footers.

The manual probe guard accepts an explicit cash reservation floor in addition
to its priced token and runtime bound. It reserves the larger amount against
the existing admission stop without changing the execution limits. Complete
dated usage and measured infrastructure release the unused hold; incomplete
evidence remains `estimated_unverified`. This accounting operation does not
authorize a live run or impose native provider execution controls.
Gemini's host termination and usage verification are separate facts. When a turn
has already failed with a rate limit, a retryable rate limit on its final usage
fetch preserves the original failure and session when retained root, model and
session evidence validates. Missing or inconsistent retained evidence refuses
reconciliation. Validated retained usage is recorded through the same accounting
path. Unknown usage stays pending; known
charges remain idempotent, and later verified revisions settle only their delta.
An unavailable final meter does not establish zero cost or release an unverified
reservation. Local storage, validation and accounting failures still propagate.


`evaluate_turn` consumes the read-only `TurnAccountingReader` API with independent
normalized native receipts, per-revision `RevisionRateAttribution` evidence and
`TurnUsageInventory`. A sealed, completely priced and settled request walk can
resolve Anthropic request-span coverage without inventing a turn observation.
The hook compares native versions/counts independently of persisted rows, joins
signed corrections once, and parses the actual posted footer. Unknown rates,
container time or inventory leave the result PENDING; a known debit cannot fill
in an unknown cost. The complete-turn path leaves the original `evaluate`
contract available and changes no posted cost or default product behavior.


The additive `FooterAccountingHook.evaluate_turn` joins that complete per-turn
snapshot to actual Discord, Slack or Teams adapter captures. Every post receipt
is retained and the platform/destination/root must match the authorized binding.
Cost comes from the accepted embed/context/Adaptive Card summary leaf, never from
answer text or the queried ledger. Once capture, inventory, rates, infrastructure
and settlement are complete, a missing required displayed cost is an executed
D11 **FAIL / POSTED_COST_MISSING**. This currently describes unchanged native
OpenAI/Gemini footers even when controlled host arithmetic passes. Unknown facts
still return typed PENDING. The older `evaluate` interface remains unchanged;
controlled component results are not live-spend or full catalog certificates.


Native OpenAI/Gemini platform footers can show settled turn cost with
`DAIMON_TURN__NATIVE_FOOTER_COST=true` (default `false`). The host must supply
verified rates for the resolved model and complete native usage, infrastructure
and current-root settlement. The displayed `used` amount includes the configured
markup and follows the ledger's per-charge rounding. Unknown measurement shows
`Cost unknown`; an incomplete total shows `Cost partial`, without a numeric
subtotal. This option supplies no rates and starts no reconciliation timer.
With the option off, native footers keep their previous appearance. Anthropic
footers keep their existing appearance and arithmetic with either value.


Admin backend cost previews use `pricing.list_price_preview(model, rates)` with
rates from the existing pricing table or the explicitly supplied native runtime
tariff. The selected model, checked date and any supplied request tier remain
visible. Anthropic prompt-length tiers and five-minute cache-write rates use the
existing table. Missing or unverified prices stay unknown or unverified; no
fallback model or free turn is inferred. These are token list prices, excluding
billing markup, hosted runtime, external tools and cache storage. The worst-case
per-turn cost remains explicitly unknown: token limits on one request do not
bound a turn's request count or other charges. A preview neither reserves spend
nor establishes a financial cap.


Host-worker accounting reads use `usage_delegation.read_delegated_usage` with
host-authorized worker bindings and resolved tariffs. The read preserves each
worker's native session, model, observation, revision and ledger identity. It
returns disjoint token floors at provider list price without markup, existing
signed customer ledger debits including markup, and pending revisions. The
floor and debit amounts overlap and must not be added. The orchestrator rollup
creates no second debit. Token floors omit unknown infrastructure and rates. A zero floor or settled latest observation
alone is not proof of complete final usage or permission to release an admitted bound.

`stores.turn_usage.read_fleet_accounting_revisions(session, tenant_id=..., run_id=...)`
reads one exact durable run and its private worker bindings in the caller's coherent
read-only transaction. It returns original usage revisions, outbox settlement facts,
customer ledger identities and persisted worker exposure without repricing or writes.
Fleet and per-binding accounting reads use `stores.read_persisted_columns` to
project authoritative database columns, including after a Core update. It refuses
effective persistent dirty/deleted state in tables the query reads, including
subqueries, with `UnflushedReadState`. Pending new objects, reverted assignments
and unrelated dirty tables remain unflushed. No caller state is overwritten.
Reserved exposure already contains observed debits; the two must not be added.
Unavailable or unaccepted bindings remain explicit gaps; a missing row never implies
zero spend. Reused root-session history is excluded. These product facts do not prove
provider final actuals or connect a fleet run to a separate QA cash receipt. That
connection needs an actual allocation-owner record, not a role, model or time match.

The same accounting reader accepts `outcome_ids` and the admitted `account_id`
for exact root attribution. The caller obtains the original outcome UUIDs from
its counted input operation associations. The query joins the scoped immutable
outcomes' `usage_refs` to original usage revisions, usage-event rows and ledger
keys, so reused-session history is excluded. Legacy request spans remain
usage-event rows; no neutral meter is fabricated. Original debit keys are
unioned once across mirrored records. Missing outcome or reference associations
stay pending; replay never substitutes a fresh observer. `attribution_complete`
certifies only this reference join, not final usage, price, runtime, settlement
or permission to release a reservation. Existing no-selector reads are unchanged.


Fleet monetary interfaces are opt-in host seams; ordinary turns do not call
them. `channel_budget.admit_fleet_worker` is the single transactional spawn and
reservation entry point. Its producer retains ambiguous charges at their whole
bound, releasing only a cancelled worker with proof it was never claimed or
allocated and has no native accounting evidence. The graph covers live or
unsettled runs plus the requested run for terminal readback; overrun latches
belong to the current run, while other holds reduce available tenant funds.

Native preparation requires fresh `require_fleet_worker_admission` readback,
including the shared tariff check. Hosts composing fleet row writers acquire
`stores.fleet.lock_budget` before any run or worker lock, then commit before
native I/O. Financial locking uses the tenant's existing ledger foreign key;
unrelated ordinary writes continue. Passive `produce_fleet_budget` refresh
returns a mapping of worker IDs to decisions from one graph pass, retaining
KEY SHARE on the tenant and account while allowing ordinary chat ledger and
turn inserts for that same tenant. Its funds snapshot is informational; only
fresh financial admission uses the tenant FOR UPDATE fence and authorizes
native allocation.

Hosts start one `watch_fleet_budget` loop per run, passing the run's mapping
of worker IDs to cancellation events and a run-finished event. Register each
worker event after its admission transaction commits. It fans out
these decisions at four Hz by default, including producer time in the interval,
and remains active across completed or cancelled cohorts until the run owner
sets the run-finished event. Unavailable usage fails closed and cancels all
registered workers. The watcher has no separate worker
deadline: `run_prepared_turn` owns it. Complete native execution, operator-funded
admission, replacement inventory and exact final settlement remain unavailable
here. These interfaces create no ledger charge or provider-native hard cap;
other runs' undebited native floors are discovered by their own refresh.


At the bound worker's before-input edge, hosts call the same
`require_fleet_worker_admission` with the actual prepared `binding`. It requires
an already funded running row and the exact current binding in that worker's
private slot, including generation, configuration revision and native IDs.
The call rechecks authorization, resolved worker tariff, priced floors and
funds under the existing financial fence; it never reserves an unfunded worker.
Callers commit refusals as well as successes before dispatch, and keep the
existing lease, single-attempt input claim and canonical model/resource proof.
Omitting `binding` keeps the unallocated starting-claim check.

The worker store records the accepted initial input once under its stable
operation identity. Financial guard recapture never increments that count.
A funded running binding with one counted turn remains eligible for fresh
checks and monitoring; the count is not proof of zero spend or final usage.
Recovery attempts, extra turns and a used turn on an unallocated row stay
refused in the single-input executor slice. The store's max-turn admission and
input claim still precede dispatch; financial facts do not authorize replay.

Optional worker request limits are part of the existing canonical intent and
must narrow the parent's limits. Missing profile support refuses a requested
limit before resource compilation; an output-token ceiling requires native
enforcement. Host turn counts and observed model usage do not substitute for
an enforced limit on internal model requests. Omitted limits preserve the
existing serialized intent.

An authenticated parent's optional `provider_limits_usd` fences each provider's
sibling worker exposure within the run. A missing or zero line refuses that
provider; a funded reservation cannot borrow unused capacity from another line.
The same financial producer sums whole unknown reservations and measured
overruns before admission, under its existing lock and exact decimal context.
These worker sublimits do not reserve root inference or install credential-line
guards. The caller must separately bind root admission and native request
controls; a financial snapshot alone never certifies a provider spending cap.

An optional `native_budget_usd` requests a native session spend control, in
positive exact cents. It is a soft control, separate from the held worker
budget. Its worker must have one host input and a deadline within 120 seconds;
the existing input claim counts that input before dispatch and retains the
count after an uncertain acknowledgement. Additional turn-input mutations are
refused, while cancellation and archive remain available. A child cannot drop
or increase a configured native budget. The driver must compile the control;
missing profile support refuses it. Omission preserves ordinary execution.
Native internal inference, token overshoot and remote cancellation delay are
not bounded by that host input count. Requested hard token or model-request
limits retain their separate unsupported-capability refusal.


For an explicitly budgeted worker, the Anthropic session composer sends the
canonical amount as positive integer USD cents in `budget.max_list_cost.amount`.
The shared lifecycle codec carries that field through the SDK's extra body for
both ordinary and repository-backed creation. The owned session's creation
acknowledgement and fresh readback must retain the same budget before execution.
The OpenAI preparation edge compiles the amount into the existing
`spend_control.limit` integer cents and rejects conflicting deployment controls
or missing, malformed or changed native echoes. Native rejection never retries
without the control. Unconfigured calls omit these additional request fields;
soft controls do not make provider-internal inference a hard bounded bill.


An explicitly budgeted worker's scoped OpenAI creation acknowledgement is retained as a provisioning receipt
before its native spend-control echo is validated. The receipt includes the
native session reference and budget echo; it does not establish a runnable
binding. A rejected acknowledgement or fresh readback refuses input and uses
the existing owned, bounded session cleanup path. Cleanup failure retains the
creation identity and financial hold for reconciliation; restart never allocates
a replacement for that receipt. Unknown transport acceptance stays unknown.

A configured provider map also requires a positive line for the financial
parent's provider. This is an admission condition; the worker exposure producer
does not thereby price or reserve root inference. Root admission, native budget,
request count, wake lifetime and trusted grant installation remain separate
caller obligations. Worker lines must fit alongside the reserved root room.

Ordinary OpenAI sessions, including deployment-level spend limits without a
worker `native_budget_usd`, preserve their prior request and readback behavior.
Only an explicit worker budget enables strict native echo validation, against
the exact integer cents sent in that session request. A deployment limit alone
does not enable provisioning receipts or budget-rejection cleanup.

The worker echo requirement is omitted from serialized controls when false.
All prior control fields, including null caps and disabled delegation, remain
verbatim in plan and creation digests. Existing ordinary bindings and retained
creation receipts therefore recover under the same stable identity. A true
worker echo requirement remains part of policy identity; no digest fallback or
replacement key is used.

Profile accounting modes select the existing settlement protocol. Request-span
profiles retain their legacy arithmetic, projections and idempotency keys.
Native totals require complete attributed usage, verified prices and measured
infrastructure before settlement; unknown charges stay pending. Native root
totals additionally permit the existing independently proven disjoint child
inventory; this does not enable delegation configuration. Usage DTOs carry only
a provider, so accounting reads the registered profiles' shared mode and refuses
an ambiguous declaration rather than picking a different billing protocol.


Production native runtime registrars obtain paid Standard direct-API text rates
from `daimon.core.pricing.provider_prices(profile)`. Their provider identities come from the existing profile owner, and validity
is evaluated against the UTC date. These immutable source rows
were checked on 2026-10-14 against the official OpenAI model/Agents usage pages
and Gemini pricing/Antigravity pages listed in `PROVIDER_PRICING_SOURCES`.
Coverage is `gpt-6-luna`, `gemini-3.5-flash-lite`, and `gemini-3.8-flash`;
unsupported models stay unpriced. Flash promotional rates expire at 2027-01-01.
These token rates exclude tool, cache-storage and container/session charges.
The runtime still needs complete measured infrastructure before exact billing.

Luna's Standard short-context row is a token floor across its context tiers.
Exact cost from this row requires inclusive input at most 272,000 tokens; a
larger turn/session aggregate cannot identify its individual request tiers and
remains unknown. Missing cache-write counters also keep exact cost unknown.
The tariff does not enforce an input cap or convert a token floor into a bill.
Fast, Batch, Flex, regional and FedRAMP pricing are outside this supplier's
request-tier contract; a registrar selecting them must refuse this supplier.

The installed OpenAI and Gemini runtime callbacks pass these same price objects
to the fleet pricing context and their native usage recorders. Default fleet
startup discovers the callbacks without credentials or provider calls. Missing
model rows do not reserve worker funds, and supplying a token tariff leaves
unknown infrastructure and incomplete native counters pending.

Root reservations use their original scoped operation and private thread slot.
The monetary owner takes the tenant and actor financial fence before reading
that operation. Financial proofs use persisted columns without autoflush;
staged inserts, edits or deletes refuse before they can become admission evidence.
Existing persisted readers retain their default pending-insert behavior.
A replay proves its complete original cash reservation and adds
no second hold. Missing, undersized, foreign or incomplete reservations refuse.
Unknown allocation keeps its original hold; a cached native receipt cannot
replace mutable financial evidence. Worker admission reads the same retained
root exposure alongside original worker reservations and ledger debits.

Explicitly budgeted private fleet roots select their finite native controls
before session compilation and reserve cash before allocation. Unsupported
native controls refuse. Distinct one-event input claims spend the root's
retained allowance atomically with the existing send claim; a replay does not
consume another input. The first active turn observer is retained with that
input, including an explicit missing association when no observer exists.
Completion wakes use the original root operation and reservation.

Allocation preserves the caller's authorization guard. New claims recheck the
current grant and original funding; revocation or exhausted cash blocks new
inputs. Stop, archive and recovery interrupts remain available to drain work.
A controlled root never silently falls back to an uncontrolled replacement.
Unknown allocation keeps its original ACK and cash hold; this does not certify
usage, final spend, or release of unknown holds. Ordinary unbudgeted turns keep
their existing preparation path.

Gemini fleet workers with explicit input/output token ceilings execute one
bounded, tool-free model interaction. The input ceiling includes cached tokens;
output includes thinking. Worst-case reservations price every input token at
the maximum applicable input/cache-write rate and every output token at the
output rate. Cache discounts can reduce actual cost but cannot expand the
worker's token authority. Existing worker cash holds, funding checks and
unknown-usage handling remain authoritative.

## Self-hosted request reconciliation

The self-hosted driver records each durable `model_request_started` as a pending
request with unknown usage (revision 1). A later measurement supersedes it as
revision 2 under the same observation id; replay never creates another charge.
An explicit `usage_unavailable` also keeps that request pending. Missing token
counts remain unknown, including after cancellation or a harness restart.

A terminal turn with no request inventory or usage is retained for operator
reconciliation. A cancellation stays interrupted; other outcomes report an error. Its terminal event remains replayable,
and later turns can use the session. Operators must investigate these unresolved
turns rather than treating them as zero-cost work.

## Selfhosted Azure usage

The selfhosted accounting API settles incremental `model_request` observations
through the transactional usage outbox and credit ledger. A stable observation
ID represents each harness request: replay, polling and corrections must retain
it. Higher revisions apply only the signed difference. Cached input is a subset
of inclusive input, and reasoning is already included in output. Markup applies
once to the resulting cost.

Azure deployment names are arbitrary. Configure
`DAIMON_BILLING__SELFHOSTED__DEPLOYMENTS` as a JSON object mapping deployments to
underlying models, and `DAIMON_BILLING__SELFHOSTED__MODELS` as a JSON object keyed
by underlying model ID. Each model contains a **list of historical tariffs**.
Each tariff requires `effective_from` (ISO date), `input`, `output` and
`cache_read` (USD per million tokens), with optional exclusive `valid_before`.
A newer effective date ends the previous open window. Retain historical entries;
duplicate dates, overlapping windows, aliases shadowing another model, empty
histories and deployments targeting unpriced models fail settings validation.
Input/output rates must be positive; cache-read can explicitly be zero. Supply
verified regional/contract Azure rates. There is no built-in Azure tariff or
public OpenAI fallback.

`require_selfhosted_price` provides a refusal gate; the runtime **must call it
before paid work**. These settings and the reserved profile do not wire that
gate or enable execution. The runtime must also call `admit_selfhosted_usage`
(which calls the gate) before model I/O, using the durable input/send idempotency
key. Admission commits the deployment, underlying model, tariff identity
(model plus effective date), rates and authorized session in the existing
operation store. Repeating that admission key retrieves its original snapshot.

`record_selfhosted_usage` takes that `admission_key`, not live tariff settings.
The first receipt persists its admission snapshot; all corrections and recovery
use the same snapshot, even if the deployment is remapped, a rate is edited,
or a newer tariff takes effect. Native usage payloads are unchanged. Receipts
may name the admitted deployment or its admitted underlying model. A missing,
late or corrupt admission, or an unexpected model, stays pending with a reason;
current settings cannot retroactively establish what was admitted.

The recorder requires an explicit `infrastructure_usd` argument: `None` means
unknown and keeps settlement pending; audited zero means the operator funds
compute separately. The token counters cannot establish compute cost. Unknown
or partial usage stays pending even with populated counters. Missing usage is
never replaced by zero. Turn telemetry retains unknown cost and unpriced calls.

Inspect pending billing observations with:

```bash
daimon usage pending TENANT_UUID --limit 1000 --json
daimon usage pending TENANT_UUID --limit 1000 --cursor PREVIOUS_NEXT_CURSOR --json
```

The read-only JSON envelope contains `rows`, `truncated`, `next_cursor` and
`invalid_rows`. Rows include a reason, such as `usage_unknown`,
`infrastructure_unknown`, `admission_missing`, `model_mismatch` or
`tariff_context_invalid`. Pages follow creation time with stable identity ties;
the cursor is tenant-bound. The default limit is 100 and maximum is 1000 per
page. A malformed body is flagged as `invalid_observation`, with no body-derived
session/model data, so other gaps remain visible. A settled higher revision
removes its superseded gap while retaining all audit snapshots.

Recovery replays authoritative harness usage through the recorder. An unchanged
pending snapshot may settle when verified compute becomes available; repaired
usage requires a higher measured revision of the same request ID. The CLI only
reports gaps: it provides no apply/repair command. Runtime journal replay owns
reconciliation. A missing admitted tariff needs authoritative original admission
evidence; admitting a new tariff after the request does not repair it.

The Anthropic usage sweep is not a selfhosted recovery path. The harness journals
request intention before model I/O. On reconcile, an unmatched intention becomes
an unknown observation with the same stable request ID. Retain it pending until
authoritative usage is recovered; never infer token counts from a terminal turn
or a missing receipt.
