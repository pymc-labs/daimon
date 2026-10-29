# Billing and credits

How Daimon meters model use, prices it and stops a turn when credit runs out.
Read it if you run a deployment that serves more than one Discord server,
Slack workspace or Teams tenant on one provider key.

Every model call is priced in USD and debited from the tenant that caused it.
By default a turn cannot start unless that tenant has credit left. Metering and
the balance gate work with no configuration. Stripe top-ups and the per-person
cap need setup (see [Turn it on](#turn-it-on)). Channel budgets stay off until
an admin sets one.

## Choose a funding mode

Every tenant is `prepaid`: a turn needs a positive balance. An
`operator_funded` tenant may run at zero or negative balance, at your cost.

```bash
daimon tenants funding-mode discord <guild-id> operator_funded
daimon tenants funding-mode slack <team-id> prepaid
daimon tenants list --json
```

Usage, debits, markup and caps still apply, so the balance can go negative as a
spend signal. Each admission at a depleted balance logs the warning
`billing.operator_funded_balance_alert` with `tenant_id` and `balance_usd`.
Route it to your log alerting. Switching
back to `prepaid` restores the gate at once and adds no credit.

## What is metered

The unit is one model call, not one turn. A turn fans out into many calls, and
each is priced and debited as it happens. Calls made while no stream was
attached, or after an interrupt or a turn ceiling, are billed when the turn
ends. A call is never debited twice.

Tool calls to a model, such as media or classification, are debited too.

## Pricing

Prices are USD per million tokens, in four dimensions: input, output, cache
write and cache read. The table holds the provider's list price.

| Model | Input | Output | Cache write | Cache read |
| --- | --- | --- | --- | --- |
| Opus 5.5 | $4 | $20 | $5 | $0.20 |
| Opus 5.5 fast | $8 | $40 | $10 | $0.40 |
| Sonnet 5.5 | $2 | $10 | $2.50 | $0.10 |
| Haiku 5.5, prompt up to 100,000 tokens | $0.10 | $0.50 | $0.125 | $0.01 |
| Haiku 5.5, longer prompt | $0.50 | $2.50 | $0.625 | $0.05 |

The prompt counts input, cache reads and cache writes. All cache writes are
priced as five-minute writes.

`DAIMON_BILLING__MARKUP` multiplies the cost before it is debited
([configuration.md](configuration.md#daimon_billing__markup)). It applies to
the ledger and to a finished turn's summary line. Usage panels and
`get_turn_cost` show provider cost, before markup.

> **Warning:** An Anthropic model with no pricing row runs free: the debit is
> zero and Daimon logs `billing.unpriced_model`. OpenAI, Gemini and
> self-hosted turns on an unpriced model are refused instead. Token rates
> exclude provider tool, container and storage charges.

## The gates

Three gates run before a turn starts, always in this order: balance, cap,
channel budget.

1. **Balance.** A prepaid tenant needs a strictly positive balance. This holds
   even with no Stripe setup, so a spent trial credit blocks the tenant.
2. **Cap.** A person's spend in the current UTC calendar month must be under
   their cap. Caps are per person with a tenant-wide default. There is no
   tenant total, per-session cap or other window.
3. **Channel budget.** The channel's spend in its window must be under its
   [budget](#channel-budgets).

A denial aborts the turn with a plain notice, such as "Your team's Daimon
credit has run out." The turn never falls back to a cheaper model. MCP tools that start a turn,
routine runs and wakes pass the same gates. Unprompted thread participation skips silently when one refuses.

A finished turn ends with one summary line: the agent, the time, what the turn
was debited and the money left, for example `Ada  12s  $0.042 used  $41.20
left`. The money left is the channel budget's remainder, else the prepaid
balance.

### Overdraft bound

The gates run once, before the turn, and debits land per model call. Nothing
re-checks mid-turn, so one long turn can take a balance negative. Concurrent
turns compound this: every turn admitted while the balance was positive runs
to completion, so a chat tenant can overdraw by up to its concurrent-turn cap
times one turn's cost. The cap is the adapter's
`max_concurrent_turns_per_tenant` (default 3), or the tenant's override from
`daimon tenants turn-cap`. A turn over the cap waits in the queue and runs the
balance gate again once it holds a slot.

MCP `start_turn` sessions are worse. Their spend reaches the ledger only when
a read sees the turn end or the sweep settles it, and MCP turns have no
concurrency cap. The overdraft is then bounded by what a tenant runs before
those reads, not by the cap.

### Exempt usage

A caller with no platform user, such as an operator, the CLI or an internal
token, runs with no balance check, no cap check and no debit. The operator
absorbs that spend. The session is stamped billing-exempt when created, so its
creator's posture covers every turn on it, even a platform user's. An exempt
caller on a billed session, such as `daimon run --session` on a chat thread's
session, is debited to the tenant.

`daimon sessions create` makes an ephemeral session with no tenant stamp and no
row, so it is never settled. It is the session `daimon run` normally targets.

To see absorbed spend, total the `usage_sweep.exempt_skipped` log lines by
distinct `managed_session_id`, taking the latest line of each.

## Channel budgets

An admin caps what one channel spends with the MCP tool `set_channel_budget`
or the CLI. With no budget there is no limit, and nothing is set by default.

```bash
daimon channels budget set <platform> <workspace-id> <channel-id> <usd>
```

Budgets work on Discord, Slack and Teams channels, with or without Stripe.
A Teams 1:1 chat has none.

- **Spend** is the channel's debits in the window, markup included. A thread
  counts toward its channel.
- **Window** is `monthly` (the UTC month), `total` (since `starts_at`, or ever)
  or `fixed` (from `starts_at` to `ends_at`).
- **The gate** trips once spend reaches the limit, so a limit of 0 stops the
  channel. A turn in progress finishes past it.
- **The notice.** The first chat turn refused in a window DMs the channel's
  admins, or the server admins if it has none. Raising the budget re-arms it.
  `DAIMON_BUDGET_NOTICES` turns it off per tenant
  ([configuration.md](configuration.md#daimon_budget_notices)).

Members read a budget with `get_channel_budget`. Listing, setting and clearing
are for server admins only. Each change is audited.

A DM moved with `/dm-me` counts toward the channel it came from, and `/dm-me`
is refused while that budget is used up. A budget does not cover:

- MCP turns from a key made outside a channel, from the hub or a bearer token.
  A key made in a channel with limited readers, or one its agent's rule names,
  is gated by and charged to that channel.
- Routines made with no channel, for example from a DM.

## Signup credit and top-ups

`DAIMON_BILLING__SIGNUP_CREDIT` (default $10) is granted once per tenant when
it is provisioned, not per person, and a reinstall cannot mint a second trial.
Set it to `0` to require payment before first use
([configuration.md](configuration.md#daimon_billing__signup_credit)).

Admins buy credit with Stripe Checkout, as one-time payments. `/billing`
offers fixed amounts, each labelled with an estimated number of turns. Admin
status is checked at click time. The checkout takes the tenant from a verified
token and the amount from Stripe, and a repeated completion event cannot add a
second top-up. Refunds and disputes debit the ledger, never more than the
original credit.

A credit that conflicts with an existing one for the same payment fails
processing and stays retryable. Inspect the Stripe event, payment and tenant
before replaying it, since a retry cannot fix inconsistent data.

Without Stripe, operators credit a tenant directly and set caps:

```sh
daimon tenants credit discord <guild-id> 25.00 --note "hackathon grant" --id grant-1
daimon tenants cap discord <guild-id> 10.00
daimon tenants cap discord <guild-id> 5.00 --user <platform-user-id>
```

Credit prints the new balance. Repeating the same arguments with the same
`--id` adds nothing, and omitting `--id` generates a new one. The cap without
`--user` is the default for everyone, and a zero cap blocks all turns.

## Promo codes

A promo code hands out credit. A **server-wide** code is redeemed by a server
or workspace admin and credits the tenant. A **channel** code
(`--channel-cap`) is redeemed by any member and credits their channel.
`/billing` shows **Redeem code** only once some code can be redeemed.

```sh
daimon promo create --amount 20                    # prints the code once
daimon promo create --amount 50 --timed \
  --starts 2026-06-01T09:00Z --ends 2026-06-03T18:00Z --max-redemptions 40
daimon promo create --amount 10 --channel-cap 30 --max-redemptions 100
daimon promo list
daimon promo redemptions <code-id>
daimon promo revoke <code-id>
```

Codes are deployment-wide and stored only as a hash, so a lost code cannot be
shown again. `--code` sets a chosen code of at least 12 characters.
`--redeem-from`, `--redeem-until` and `--max-redemptions` bound when and how
often it can be redeemed. A tenant redeems a server-wide code at most once.

- **Credit** codes add ordinary credit at once. It never expires.
- **Channel** codes need `--channel-cap` of at least `--amount`, and cannot be
  timed. Each person redeems once per channel, until the channel has taken the
  cap. With `--amount 10 --channel-cap 30`, three people can each add $10. The
  credit also raises that channel's budget limit. They cannot be redeemed in a
  DM, a group DM, a Teams 1:1 chat or by MCP tool.
- **Timed** codes grant only between `--starts` and `--ends`. After the window
  closes, what was not spent is removed. Spend inside a window draws on timed
  credit first, soonest expiry first.

A refund credits back spend recorded after a window closed, about fifteen
minutes later. Until then the balance can read low or trip the gate.

Five refused redemptions in 15 minutes pause that person's attempts only. Every
attempt is audited. Revoking stops new redemptions only, and credit already
redeemed stays.

An integration can issue codes over MCP with an operator token holding
`promo:create`, using `create_promo_code`, `list_promo_codes` and
`revoke_promo_code`. A token minted with `--max-issued-usd` can issue at most
that much in total, counted as `amount_usd × max_redemptions` per code.

## What admins see

`/billing` on Discord and Slack, and `billing` on Teams, shows the current
calendar month.

- **Members** see their own use against their cap and the credit left.
- **Admins** also see the month's spend, the top five spenders, the five most
  used channel budgets, **Add credit**, **Redeem code** and a **Look up a
  person** picker.
- **Expiry dates** list timed credit with its end date, soonest first.

Panel figures are before markup. Tenant totals leave out usage with no platform
user, and exempt usage appears only in the settle logs. For one finished turn,
`get_turn_cost` returns a raw, pre-markup figure, and `None` rather than zero
for an unpriced model.

To list measured turns, without prompts or answers:

```bash
daimon usage turns <tenant-uuid> --days 7 --json
```

Add `--channel <channel-id>` or `--origin routine` to filter, or `--summary`
to group by platform, channel and origin. It reads the local database and is
telemetry, not an accounting source. The ledger is authoritative for charges.

## Turn it on

Policy settings are nested under `DAIMON_BILLING__`
([configuration.md](configuration.md#billing-policy)). The Stripe secrets are
flat variables in [their own block](configuration.md#billing-stripe).

Stripe billing needs all the Stripe variables together. If any is missing,
Daimon logs that Stripe billing is disabled and mounts no checkout or webhook
routes. Metering, the balance gate, caps and the signup credit still work.

Self-service top-ups also need `DAIMON_MCP__PUBLIC_URL` and
`DAIMON_MCP__JWT_SECRET` for the hop from the chat adapter to the MCP server
([public URL](configuration.md#daimon_mcp__public_url),
[JWT secret](configuration.md#daimon_mcp__jwt_secret)). The image must carry
the optional `billing` extra, which installs `stripe`.

## Recover unbilled usage

A settle bills calls no live stream metered. It runs at the end of a chat turn
and whenever an MCP read sees a headless turn end. A scheduler sweep, on by
default, settles headless sessions nobody read again. Set
`DAIMON_SCHEDULER__USAGE_SWEEP_ENABLED=false` to turn it off
([configuration.md](configuration.md#daimon_scheduler__usage_sweep_enabled)).

> **Warning:** A settle bills every session whose tenant stamp names a tenant
> in its own database, and a session carries no deployment identity. Two
> deployments sharing one Managed Agents workspace and the same Discord server
> or Slack workspace could debit each other's sessions. Give each deployment
> its own Managed Agents workspace.

Calls from before settling existed are the backlog, which no automatic settle
reads. Settle it for internal tenants, as a dry run first:

```bash
daimon usage settle-backlog --internal <tenant-uuid>
daimon usage settle-backlog --internal <tenant-uuid> --apply
```

An internal tenant's calls are debited. Every other tenant's are waived and
logged as `billing.backlog_waived`. Applying twice debits once. The command
prints a receipt (JSON with `--json`) and writes no files, so keep the output.

## Self-hosted and Azure models

A self-hosted harness, such as Azure, is billed from measured token counts per
request. Configure `DAIMON_BILLING__SELFHOSTED__DEPLOYMENTS` to map deployment
names to underlying models. Configure `DAIMON_BILLING__SELFHOSTED__MODELS` with
each model's tariff history ([deployments](configuration.md#daimon_billing__selfhosted__deployments),
[models](configuration.md#daimon_billing__selfhosted__models)). There is no
built-in Azure tariff, so supply your own verified rates.

Usage Daimon cannot measure stays pending and is never charged as zero. List
pending items with:

```bash
daimon usage pending <tenant-uuid> --limit 1000 --json
```

Each row carries a reason such as `usage_unknown`, `infrastructure_unknown`,
`admission_missing`, `model_mismatch` or `tariff_context_invalid`. Pass
`--cursor <next-cursor>` for the next page. The command is read-only. The
runtime repairs gaps by replaying the harness's measured usage, and a tariff
added after a request cannot repair it. Never treat pending turns as free.
