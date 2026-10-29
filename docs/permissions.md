# Permissions

What each channel rule, agent rule and tool rule limits, who can change them,
and which turns are held to one channel. Read it before you restrict a channel,
pin an agent to one, or let clients into a shared channel.

Two rules say what reaches whom. A **channel rule** says whose turns may read a
channel and who may write in it. An **agent rule** says where an agent may run.
Every limit below follows from these two. Who owns what is on
[isolation.md](isolation.md).

## Channel rules

| Field | Value | Meaning |
| --- | --- | --- |
| `readers` | `any` | any turn, from anywhere (platform permissions still apply) |
| | `inside` | only turns inside the channel |
| | `own` | only the channel's own agents, inside it |
| `writers` | `any` | any agent answers and posts |
| | `own` | only the channel's own agents answer and post |
| | `none` | no turn starts and nothing posts, admins and Daimon's notices included |

A channel's **own agents** are those whose agent rule names it alone, while its
readers are `own`. That channel is their **home**. A caller with no agent, such
as the CLI or an operator token, may still post where `writers` is `own`. The
readers rule keeps that input inside.

A rule sits on a channel or a Discord category, and a thread follows both. A
category only takes `writers: none`. Chat and the Permissions screens set
channel rules only. The operator can keep one Slack or Discord thread to turns
inside it with `daimon channels rule set --thread <id> --readers inside`. A
thread takes the strictest of its own, its channel's and its category's rule.

On Discord, a direct @mention in a `writers: none` channel gets a lock
reaction and a DM naming the rule, so "not allowed here" does not look like
"Daimon is down". It is bounded: at most one notice per person per channel
within
[the cooldown](configuration.md#daimon_discord__protected_channel_notice_cooldown_min),
nothing is posted in the channel, no turn runs, and failed lookups stay silent.

## What a channel rule limits

| Limit | `readers: inside` | `readers: own` | `writers: none` |
| --- | --- | --- | --- |
| Turns start | yes | own agents only | no |
| Agents that answer and post | any | own | none |
| Read from outside (channel, sessions, history) | no | no | yes |
| Sessions read and continued by | turns inside | own agents inside | anyone allowed |
| Memory writable | no | own agents only | yes |
| Content kept inside | no | yes | no |
| Agents listed there | all | own only | all |
| Default agent | any | own only; never cleared | any |
| Charged to its budget | its turns | its turns and its own agents' calls | its turns |

**Content kept inside** means a call from a turn in the channel, whatever agent
runs it, reads only the channel and the conversations that ran there. It posts
only inside it, sends no direct messages and creates no agents. A call by the
channel's own agent is held the same way, wherever it runs. Routines saved there
post only there and never DM their results. Only the channel's setup thread may
still configure its own agent.

**Publishing** puts content behind a link that whoever holds it opens. From a
channel whose content is kept inside, or by an agent with a rule, it waits for
the requester to press Approve on a card (see [Publishing](#publishing)).

**Listed** covers agents, their skills, environments, routines, timers and
defaults. From outside, a channel's own agents are hidden everywhere.

A thread whose channel is unknown fails closed while any channel's readers are
`own`, since it may lie in one.

## Agent rules

`runs_in` lists the only channels an agent runs in, threads included. It is
unset for an agent that runs wherever the [config cascade](architecture.md)
sends it, and empty for one that runs nowhere. A rule on any name the agent
carries applies, so an agent with rules under two names runs only where both
allow.

| | no rule | `runs_in` | own agent (home) |
| --- | --- | --- | --- |
| Runs | where the cascade sends it, outside `readers: own` channels | inside `runs_in`, outside `readers: own` channels | inside its home |
| Posts | where it runs and `writers` allow, and the requester's own DM | the same | inside its home only |
| Direct messages | anyone | the requester only | none |
| Publishes | yes | once the requester approves | once the requester approves |
| Creates agents | yes | yes | no |
| Copied (fork) | by an admin | never | never |
| Listed | outside `readers: own` channels | the same | inside its home only |
| Memory writable | where `readers` is `any` | the same | also in its home |
| Charged to | the channel it runs in | the same | its home |

## Agent tool rules

A current human workspace admin can set `allow`, `ask` or `deny` for one
third-party MCP tool on one agent:

```bash
daimon agents tool-rules --help
```

The MCP tool `set_agent_tool_rule` does the same. The key is exactly
`server/tool`, bound to the agent and the server URL attached under that name.
Changing either makes the rule stop matching. Only a person can edit rules: an
agent key, or an agent acting for an admin, is refused. Every edit is audited.
Rules are inactive until both `DAIMON_TOOL_SAFETY__ENABLED`
([configuration.md](configuration.md#daimon_tool_safety__enabled)) and
`DAIMON_TOOL_SAFETY__AGENT_RULES`
([configuration.md](configuration.md#daimon_tool_safety__agent_rules)) are true.

- **Scope.** An agent-wide rule applies wherever the agent runs. A channel rule
  applies in its channel and its threads. `deny` wins over `ask`, which wins
  over `allow`, and the operator's deny list wins over all.
- **Chat versus routines.** `chat` is the default scope. An `allow` reaches
  unattended routines only with `chat_and_routines`. `ask` asks in chat even
  for a read and refuses unattended. `deny` always refuses.
- **Always allow.** With both settings on, a card for a third-party write also
  shows **Always allow**. A current admin, who may be someone other than the
  requester, can press it to save an agent-wide `allow` for chat. A non-admin's
  click leaves the card pending.

Daimon's own tools take rules under the key `daimon-mcp/<tool>`. An agent-wide
`deny` on `daimon-mcp/*` denies every Daimon tool for that agent, including
tools added later. Wildcards work only there, and only as an agent-wide
`deny`. Operator tokens and agent keys are not affected.

A rule on a publish tool makes it ask in every channel and refuse in routines,
even where the rule does not name the channel.

> **Warning:** Gemini and OpenAI cannot pause for a card. A turn for an agent
> on one of them is refused when the agent has an `ask` rule or a
> channel-scoped `deny` on a Daimon tool. Agent-wide `deny` rules still work.
> The person sees the same "this agent can't run here" notice as for any
> unusable backend, and the operator log records `turn.backend_unsupported`.

## Change agent setup

Server or workspace admins may change an agent's setup, and so may the agent's
**owners**. The first owner is the person who created or forked the agent. A
channel's own copy keeps that agent's owner. Nobody from another organisation
can be an owner, and agents created before owners existed have none and stay
admin-only. Manage owners with:

```bash
daimon agents owner set <agent> <person>
daimon agents owner add <agent> <person>
daimon agents owner remove <agent> <person>
daimon agents owner show <agent>
```

`daimon agents fork <agent> [<new-name>]` copies an agent without its MCP
credentials.

A co-owner can do everything the primary owner can. An owner may also manage
GitHub repos for their own agent only:

| | Admin | Owner of the agent | Anyone else |
| --- | --- | --- | --- |
| Connect repos to the agent | yes | yes | asks an admin |
| Remove any repo from the agent | yes | yes | no |
| Choose the agent's working repo | yes | yes | no |
| Connect a repo for every agent on the server | yes | no | no |
| Installation notices, GitHub home, new-repo cards, approvals | yes | no | no |
| Add or remove owners | yes | no | no |

Removing a repo from one agent leaves a server-wide repo connected for the
others. Owners cannot change what the whole workspace shares: the provider
allowlist, channel budgets, server-wide repos, deleting a skill from the
workspace, archiving or copying the agent, and its tool rules stay with
admins.

When approval cards are off, a skill add needs a preview followed by the
requester's next message of `yes` in that thread. A newer preview replaces the
previous one.

## Use coding keys

**Use from your coding tools** on an agent's Details makes a key that lets
Claude Code or another coding tool talk to that one agent. A server admin can
make one for any agent, and an agent's owner for that agent only. Teams offers
it to admins only.

The key acts as the person who made it, with their ordinary member powers and
never an admin's. Its holder sees only the agent-chat tools. Turns are billed
to, and capped for, that person's account. An admin's key lasts 90 days and an
owner's 30.

Every request re-checks who made the key: they must still be an admin or own
that agent. Demoting an admin or removing an owner stops their keys on the next
request. Leaving the server does not revoke a key, so an admin removes them as
owner. A session already running keeps its one-hour credential.

**Coding keys** on Details lists an agent's live keys, never the token (shown
once). An owner sees and revokes only their own keys. An admin sees and revokes
every key for the agent.

## Publishing

A turn whose admission would refuse publishing gets its publish tools set to
ask first. The platform shows an Approve card naming the action and who may
answer, with the consequence and a few plain inputs behind Details. Approve and
Deny work only for the requester. The card expires after ten minutes. Agent
keys, runs nobody watches and sessions without the card are refused, so a
failure refuses rather than publishes.

## Held turns

A turn is **held** to the channel it was asked in when the requester belongs to
another organisation than the one that added Daimon (a *client*), or when the
channel is shared with another organisation. A requester whose organisation the
platform cannot determine is treated as a client for that turn only.

A held turn reads and posts only inside its channel and its threads, and reads
only sessions that ran there. It cannot send direct messages, save routines,
create agents or start workers. Publishing needs the requester's Approve. No
admin exemption applies. A client also cannot change any agent, channel, budget
or rule, and cannot start a turn in a DM, hub, routine or agent chat. Daimon
answers them in the channel they share.

A person verified as belonging to another organisation is marked foreign, and
the mark is never cleared. A channel found to be shared is remembered, so every
later turn in it is held. The hold travels in the turn's MCP credential and is
applied before any rule. The default agent and agents bound to several channels
still run inside a held channel.

## Exemptions

- **Server admins** are exempt where the output reaches them alone: their own
  DM and hub turns, and hub reads of sessions inside limited channels. They
  configure agents with rules from anywhere and see every agent listed. Agent
  rules and `readers: own` still hold for them in channels, threads, routines
  and handoffs. Nothing writes where `writers` is `none`.
- A **setup thread** under a `readers: own` channel answers as the built-in
  agent.

Agent keys and tokens with no person behind them are never exempt, and no
exemption applies to a held turn. The [trust model](architecture.md#trust-model)
has the full table.

## Change the rules

Channel and agent rules are server or workspace admins' calls. Set them from:

- chat: `set_channel_rule` and `set_agent_rule` ([MCP tools](mcp-tools.md));
- panels: a channel's **Permissions** screen, from Who answers where in the
  Discord and Slack setup panels, and the Channel settings dialog on Teams;
- the CLI.

```bash
daimon channels rule set --help
daimon agents rule set --help
daimon tenants access-policy rules <platform> <workspace-id> --json
```

The last command lists every channel, category and agent rule, and each
agent's home.

Setting `readers: own` makes the channel's default agent its own, or copies an
agent for it (`copy_from`). Moving readers off `own` keeps its agents' rules, so
they still run only there, unless `release_agents` drops them. A write that no
rule would enforce is refused.

The invoker allowlist, read-only DM memory, environments, budgets and operator
tokens are not channel or agent rules. They say who may act and who may change
the rules.
