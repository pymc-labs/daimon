# Permissions

Two kinds of rule say what reaches whom. A **channel rule** says who may read
a channel and who may write in it. An **agent rule** says where an agent may
run. Protected, sealed, confidential (isolated) and pinned are presets of
these two rules, not separate mechanisms. `daimon.core.permissions` holds the
model. `daimon.core.authz.authorize` decides every action from the same
policy, and tests check the model against it over every combination of
presets.

## Channel rules

A rule sits on a channel, a thread (a Discord thread id, or a Slack thread as
`channel:ts`) or a Discord category. A thread takes the strictest of its own
rule, its channel's and its category's.

| Field | Value | Meaning |
| --- | --- | --- |
| `readers` | `anyone` | any turn may read it (platform permissions still apply) |
| | `inside` | only a turn inside it may read it |
| | `own_agents` | only its own agents, inside it |
| `writers` | `any_agent` | any agent may answer and post |
| | `own_agents` | only its own agents answer and post; a caller with no agent (the CLI, an operator token) may still post |
| | `nobody` | no turn starts and nothing posts, admins and daimon's own notices included |

A channel's **own agents** are the agents pinned to it alone. `own_agents`
is set on both fields or neither, except that a confidential channel may
also be protected.

## Presets

| Preset | `readers` | `writers` | Formerly | Panels show |
| --- | --- | --- | --- | --- |
| open | `anyone` | `any_agent` | | |
| protected | `anyone` | `nobody` | protected channel or category | |
| sealed | `inside` | `any_agent` | sealed channel | Private |
| confidential | `own_agents` | `own_agents` | isolated channel | Hidden |

Anything else is a mix: a sealed channel that is also protected has
`readers: inside` and `writers: nobody`. A category can only be open or
protected.

## Agent rules

`runs_in` lists the only channels an agent runs in, threads under them
included. It is unset for an agent that runs wherever the
[agent cascade](architecture.md) sends it, and empty for one that runs
nowhere. A pin on any name the agent carries applies, so an agent pinned
under two names runs only where both pins allow.

Everything else follows from `runs_in` and the rule of the channel the agent
belongs to:

| | Unpinned | Pinned | Own agent of a confidential channel |
| --- | --- | --- | --- |
| Runs | where the cascade sends it | inside `runs_in` | inside its channel |
| Posts | where the channel's `writers` allow | inside `runs_in`, or the requester's own DM | inside its channel only |
| Direct messages | anyone | the requester only | nobody |
| Listed | everywhere | everywhere | only inside its channel |
| Copied | by a server admin | never | never |

Memory mounts read-only in a place whose `readers` is not `anyone`, except
for a confidential channel's own agents inside it. A turn inside a
confidential channel holds whatever agent runs it to that channel.

## Who the rules bind

Rules bind members, agent keys and tokens alike. A server admin is exempt
only where the output reaches them alone (their own DM and hub), and a
confidential channel's admins likewise for its own agents.
[Trust model](architecture.md#trust-model) has the full table.

The invoker allowlist, read-only DM memory, channel admins, environments,
budgets and operator tokens are not channel or agent rules. They say who may
act and who may change the rules, and work as before.

## Seeing and changing the rules

`daimon tenants access-policy rules PLATFORM WORKSPACE_ID [--json]` lists
every channel, category and agent rule with its preset. The rules are written
as before: `daimon tenants access-policy set`, the isolation tool and panels,
and `with_channel_rule` / `with_agent_rule` in code. The stored policy keeps
its shape.
