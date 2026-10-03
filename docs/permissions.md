# Permissions

Two rules say what reaches whom. A **channel rule** says which agents may
read a channel and which may write in it. An **agent rule** says where an
agent may run. Protected, sealed, confidential (isolated) and pinned are
presets of these two rules, not separate mechanisms.

`daimon.core.permissions` is the model. It reads the stored policy as rules,
writes rules back, and derives every limit below from them.
`daimon.core.authz.authorize` decides each action through it. Tests check the
two against each other for every combination of rules on two channels, a
thread, a category and two pinned agents.

## Channel rules

Both fields use one scale: `any`, `inside`, `own`, `none`.

| Field | Value | Meaning |
| --- | --- | --- |
| `readers` | `any` | any agent, from anywhere (platform permissions still apply) |
| | `inside` | any agent, from a turn inside the channel |
| | `own` | only the channel's own agents, inside it |
| `writers` | `any` | any agent answers and posts |
| | `own` | only the channel's own agents answer and post |
| | `none` | no turn starts and nothing posts, admins and daimon's notices included |

A channel's **own agents** are those pinned to it alone (see
[agent rules](#agent-rules)). `own` goes on both fields, or on `readers`
with `writers: none`. A caller with no agent (the CLI, an operator token)
may still post where `writers` is `own`: that is input, and the readers
rule keeps it inside.

A rule sits on a channel, a thread or a Discord category. A thread takes
the strictest of its own rule, its channel's and its category's. A category
is only open or protected. A Slack thread is only sealed, by its
`channel:ts` key; Teams channel ids take any rule.

| Preset | `readers` | `writers` | Stored as | Panels show |
| --- | --- | --- | --- | --- |
| open | `any` | `any` | | |
| protected | `any` | `none` | protected channel or category | |
| sealed | `inside` | `any` | sealed channel | Private |
| confidential | `own` | `own` | isolated channel (also sealed) | Hidden |

Any other pair is a mix, such as a sealed protected channel
(`inside`/`none`).

## What a channel rule limits

Each limit is one property of `channel_permissions(...)` or one function of
the model, so every caller asks the same question.

| Limit | open | protected | sealed | confidential | Model |
| --- | --- | --- | --- | --- | --- |
| Turns start | yes | no | yes | yes | `writers` |
| Agents that answer and post | any | none | any | own | `answers`, `runs_at`, `posts_at` |
| Read from outside (channel, sessions, history) | yes | yes | no | no | `readable_from`, `session_readable_from` |
| Sessions read and continued by | anyone allowed | anyone allowed | turns inside | own agents inside | `session_confidential_channels` |
| Memory writable | yes | yes | no | own agents only | `memory_writable` |
| Content kept inside | no | no | no | yes | `keeps_content`, `held_to` |
| Agents listed there | all | all | all | own only | `lists`, `listed_at` |
| Default agent | any | any | any | own only; never cleared | `binding_refusal`, `clear_refusal` |
| Charged to its budget | its turns | its turns | its turns | its turns and its own agents' calls | `budget_channel` |

**Content kept inside** means a call from a turn in the channel, whatever
agent runs it, posts only inside the channel, sends no direct messages,
creates no agents and publishes nothing. Routines saved there post only
there and never DM their results (`keeps_routine_inside`). Only the
channel's setup thread may still configure its own agent.

**Listed** covers agents, their skills, environments, routines, timers and
defaults. From outside, a confidential channel's own agents are hidden
everywhere.

A thread whose parent is unknown fails closed while any channel is
confidential, since it may lie in one (`confidential_unknown`).

## Agent rules

`runs_in` lists the only channels an agent runs in, threads included. It is
unset for an agent that runs wherever the [agent cascade](architecture.md)
sends it, and empty for one that runs nowhere. A pin on any name the agent
carries applies, so an agent pinned under two names runs only where both
pins allow.

`agent_permissions(...)` derives the agent's **kind** from its rules:

- **free**: no pin.
- **pinned**: pinned, but not to one confidential channel alone.
- **own**: pinned to one confidential channel alone (`own_channel`).

| | free | pinned | own |
| --- | --- | --- | --- |
| Runs | where the cascade sends it, outside confidential channels | inside `runs_in`, outside confidential channels | inside its channel |
| Posts | where it runs and `writers` allow, and the requester's own DM | the same | inside its channel only |
| Direct messages | anyone | the requester only | none |
| Publishes (reports, upload URLs, display identity) | yes | no | no |
| Creates agents | yes | yes | no |
| Copied (fork) | by an admin | never | never |
| Listed | outside confidential channels | outside confidential channels | inside its channel only |
| Memory writable | where `readers` is `any` | the same | also in its channel |
| Charged to | the channel it runs in | the same | its channel |

## Exemptions

- **Server admins** are exempt where the output reaches them alone: their own
  DM and hub turns, and hub reads of sealed sessions. They configure pinned
  agents from anywhere and see every agent listed. Pins and confidential
  channels still hold for them in channels, threads, routines and handoffs.
  Nothing writes into a protected channel.
- **Channel admins** of a confidential channel have the same exemption for
  its own agents. They configure an agent pinned only inside the channels
  they administer, and read those channels' sealed sessions from the hub.
- A **setup thread** under a confidential channel answers as the built-in
  agent.

Agent keys and tokens with no person behind them are never exempt.
[Trust model](architecture.md#trust-model) has the full table.

## Changing the rules

Protection, seals, isolation and archiving an isolation copy are server
admins' calls, never a channel admin's. Pins are written by isolation and by
`daimon tenants access-policy set`. Isolating a channel makes its default
agent its own, or copies it; a sealed channel can't be unsealed while it is
confidential. Every writer goes through `with_channel_rule`,
`with_category_rule` and `with_agent_rule`, so the stored lists never drift
from the rules. The stored policy keeps its shape.

`daimon tenants access-policy rules PLATFORM WORKSPACE_ID [--json]` lists
every channel, category and agent rule, with each channel's preset and each
agent's kind.

The invoker allowlist, read-only DM memory, channel admins, environments,
budgets and operator tokens are not channel or agent rules. They say who may
act and who may change the rules.
