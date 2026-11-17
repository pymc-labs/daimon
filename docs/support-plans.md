# Support plans

**Ask a person** sends a request from a conversation to your support team.
Support plans decide how many credits people have, what each request costs and
which options they pick from. Without a plan, each person has
[`DAIMON_SUPPORT__CREDITS_PER_USER`](configuration.md#daimon_support__credits_per_user)
credits in total, and a request costs one.

## Where a plan applies

A plan is set on the workspace, on one agent or on one channel. A request uses
the first plan that exists for the place it was asked in:

1. the channel's plan, where a thread uses its parent channel's;
2. the agent's plan;
3. the workspace plan;
4. the default allowance.

Plans never stack. A request is charged to one plan only, and plan requests
never count toward the default allowance. A request from a workspace admin is
sent but charged nowhere. To hide Ask a person in one place, turn its
[feature](features.md#ask-a-person) off there. To offer it only where a plan
applies, turn it off in the channels without one: zero credits per user turns
it off everywhere, plans included.

## Credits and windows

Credits are decimal amounts with up to two places, such as `0.25`. A plan has
shared credits, credits per person, or both, and the stricter one wins. Shared
credits are one pool for everyone the plan covers.

A plan counts the credits used in its window:

| Window | Counts |
| --- | --- |
| `total` | everything since its start date, or ever |
| `monthly` | the current UTC calendar month |
| `fixed` | from its start date until its end date |

An admin sets the dates by hand; a plan never starts on its first request.
Before its start or after its end, a plan blocks requests. It never falls back
to the default allowance. Clear the plan to restore the default.

## Own caps

An own cap gives one person a different limit under one plan, in place of its
credits per person. A cap set with no plan changes that person's default
allowance. Caps name the person by their platform user id.

## Menus

A menu lists the options a person picks from, each with a price and up to four
form fields. A plan with no menu offers one option at one credit. A menu takes
1 to 10 options, with labels of at most 75 characters.

| Price | Charge |
| --- | --- |
| `fixed` | the option's amount |
| `steps` | the step the person picks, such as 30 or 60 minutes |
| `range` | set by staff between `min` and `max` |
| `quoted` | set by staff, from an optional `min` up to an optional `max` |

Fields are `text`, `paragraph` or `choice`. A menu file is YAML or JSON:

```yaml
name: Support options
options:
  - key: clarify
    label: Clarify a question
    price: {kind: fixed, amount: "0.25"}
    fields:
      - {key: question, kind: paragraph, label: Your question, max_chars: 1000}
  - key: review
    label: Review a model
    price: {kind: range, min: "0.5", max: "1"}
```

A menu that a plan uses cannot be removed.

## Settling requests

A `range` or `quoted` request holds its minimum until staff settle it. Anyone
in the support channel settles it from the request's post, or declines it to
refund the hold. The person who asked is not told the final charge, so tell
them yourself. Every settle is recorded with who made it. On Slack, with
`slack_escalation_team_id` set, settling works only in that workspace; with it
unset, only someone from the workspace the request came from can settle. Clearing
a plan declines its pending requests.

## Change plans

Admins change plans, own caps and menus under **Channels** > **Support** in the
web app. Every change is recorded in the audit log, and applies to the next
request with no restart.

The same changes work from the command line. A thread id resolves to its parent
channel.

```sh
daimon support --guild 123 catalog set support support.yaml
daimon support --guild 123 plan set channel 456 --pool 2 --window monthly --catalog support
daimon support --guild 123 allowance set 789 5 --plan channel:456
daimon support --guild 123 requests list --pending
daimon support --guild 123 requests settle <request-id> 1.5
```

`plan set` changes only the options you pass; `none` clears a limit or a date.
Each `set` takes `--expected` with the value you last read, and refuses when it
changed meanwhile. `requests list --undelivered` shows requests the support
channel never received, to settle or decline. The `support_*` tools in
[MCP tools](mcp-tools.md) do the same from an MCP client.
