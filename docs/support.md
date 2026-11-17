# Ask a person

**Ask a person** sends a request from a conversation to your support team.
Limits decide how many credits people have, menus decide what a request costs
and which options people pick, and a channel decides where requests land.
Without any setup, each person has
[`DAIMON_SUPPORT__CREDITS_PER_USER`](configuration.md#daimon_support__credits_per_user)
credits in total, and a request costs one.

## Set it up

Admins set everything under **Admin** > **Support** in the web app. Every
change is recorded in the audit log and applies to the next request.

1. Turn Ask a person on for the workspace.
2. Choose where it shows, for an agent or a channel.
3. Pick the channel that receives requests.
4. Set credit limits for everyone, an agent, a channel or one person.
5. Write menus of options, and pick which place offers which menu.

The page's sections follow this order; the switches are also in
[features](features.md#ask-a-person). Without step 3,
requests go to the channel your deployment keeps. Without step 4, the default
credits apply. Without step 5, a request costs one credit.

## Request channel

Requests go to one channel per platform. Where the deployment allows it,
a workspace picks its own channel. Otherwise the deployment's channel
receives every request. Changing the channel moves only new requests;
open ones stay where they were posted.

```sh
daimon support channel set 456
daimon support channel show
daimon support channel reset
```

On Teams, add `--team` and `--service-url` to `channel set`. A thread id
resolves to its parent channel.

## Limits

A limit sets credits for a place: the workspace, one agent or one channel.
Credits are decimal amounts with up to two places, such as `0.25`. A limit has
shared credits, credits for each person, or both, and the stricter one wins.
Shared credits are one pool for everyone at the place.

| Window | Counts |
| --- | --- |
| `total` | everything since its start date, or ever |
| `monthly` | the current UTC calendar month |

A limit also has an optional start date and end date. An admin sets both by
hand; a limit never starts on its first request. Before the start or from the
end, requests at that place are refused. They never fall back to the default
credits. The end date itself is not counted.

```sh
daimon support limit set channel 456 --shared 20 --each-person 2 --window monthly
daimon support limit set agent helper --each-person 1 --ends-at 2030-06-01T00:00
daimon support limit list
daimon support limit clear channel 456
```

`limit set` changes only the options you pass; `none` clears one. Each `set`
takes `--expected` with the revision you last read, and refuses when it
changed meanwhile.

## How limits combine

A request is charged to one place: the most specific one that has a limit.

| Place | Wins over |
| --- | --- |
| channel | agent, workspace, everyone |
| agent | workspace, everyone |
| workspace | everyone |
| everyone | nothing; the default credits |

Limits never stack. A request counts only at the place that is charged. Admins
are never charged, but like everyone they are refused outside a limit's dates.
After a purge, a person's own count starts again; shared credits still bind.
A person's own limit sets one person's cap at a
place, or everywhere. It replaces the place's credits for each person for that
person, and never adds credits to the place. A person's cap at a channel beats
their cap everywhere.

Name the person by platform user id or `<@id>` mention: a Discord number, a
Slack member id or a Teams Entra object id.

```sh
daimon support limit set workspace --each-person 5 --person 112233445566778899
daimon support limit set channel 456 --each-person 1 --person 112233445566778899
```

## Menus

A menu lists the options a person picks from, each with a price and up to four
form fields. A place with no menu offers one option at one credit. A menu takes
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

Save the menu under a key, then pick it for a place. A menu that a place
uses cannot be removed.

```sh
daimon support menu set support support.yaml
daimon support menu use support channel 456
daimon support menu use default channel 456
```

## Settle requests

A `range` or `quoted` request holds its minimum until staff settle it. Anyone
in the support channel settles it from the request's post, or declines it to
refund the hold. The person who asked is not told the final charge, so tell
them yourself. Every settle is recorded with who made it. On Slack, with
`slack_escalation_team_id` set, settling works only in that workspace; with it
unset, only someone from the workspace the request came from can settle.
Clearing a limit declines its pending requests.

```sh
daimon support requests list --pending
daimon support requests settle <request-id> 1.5
daimon support requests list --undelivered
```

`--undelivered` shows pending requests no support channel received, to settle
or decline. The `support_*` tools in [MCP tools](mcp-tools.md) do the same from an
MCP client.
