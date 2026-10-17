# Permissions

## Agent tool rules

A current human workspace admin can set `allow`, `ask` or `deny` for one
third-party MCP tool on one agent with `daimon agents tool-rules` or the
`set_agent_tool_rule` MCP tool. A channel admin can set and clear such rules
for any agent in a channel they administer, using `--channel` or `channel_id`;
they cannot set an agent-wide rule or one for another channel. An agent-wide
rule applies where the agent runs; a channel rule applies in its parent
channel and threads under it. When both match, `deny` wins over `ask`, which
wins over `allow`. The key is exactly `server/tool`. A rule is
bound to the agent's stable id and the server URL attached under that name;
changing either makes the rule stop matching. `chat` is the default scope.
An `allow` rule reaches unattended routines only with `chat_and_routines`.
An `ask` rule asks in chat even for a read and refuses unattended; `deny`
always refuses. The operator's deny list takes precedence. Clearing leaves
an audit row and a revision marker. Every edit records who changed it, when,
and the before and after values. Only a person can edit these rules: an agent
key, or an agent acting in a conversation on an admin's behalf, is refused. They
are inactive until both `DAIMON_TOOL_SAFETY__ENABLED` and
`DAIMON_TOOL_SAFETY__AGENT_RULES` are true.

With both settings on, a card for a third-party MCP write classified as needing
approval also has **Always allow**. A current workspace admin can press it to
save an agent-wide `allow` rule for chat. A channel admin can press it in a
channel they administer to save a rule for that parent channel and its threads.
The admin may be someone other than the requester. The click approves the
pending call and saves the rule for that agent, tool and attached server URL.
The admin's permission is checked when they click. A non-admin's click leaves
the card pending; Approve and Deny still belong to the requester. If another
edit changed the rule while the card waited, the click approves this call but
does not save the rule. An explicit `deny` set while a card waits refuses the
call before release, even if the card was approved.

Two rules say what reaches whom. A **channel rule** says whose turns may read
a channel and who may write in it. An **agent rule** says where an agent may
run. Every limit below follows from these two.

`daimon.core.permissions` is the model. It reads the stored policy as rules,
writes rules back, and derives every limit from them.
`daimon.core.authz.authorize` decides through it and adds who is asking:
admin exemptions and agents it couldn't resolve. Code that only asks whether
any rule exists, or where, asks the model (`any_own_readers`,
`any_readers_limited`, `home_of`, `ruled_agents` and the like), never the
stored rules. Tests check the model against `authorize` over combinations of
rules on two channels, a thread, a category and two agents with rules.

## Channel rules

| Field | Value | Meaning |
| --- | --- | --- |
| `readers` | `any` | any turn, from anywhere (platform permissions still apply) |
| | `inside` | only turns inside the channel |
| | `own` | only the channel's own agents, inside it |
| `writers` | `any` | any agent answers and posts |
| | `own` | only the channel's own agents answer and post |
| | `none` | no turn starts and nothing posts, admins and Daimon's notices included (on Discord, a mention gets only a 🔒 reaction and a DM; see below) |

On Discord there is one narrow exception, so that "not allowed here" doesn't
look like "Daimon is down". A direct @mention of Daimon in a `writers: none`
channel gets a 🔒 reaction, and its author a direct message naming the rule.
For a channel rule, the message says a server admin can change it in
`/agent-setup` → **Who answers where** → **Permissions**. For a category rule,
it says whoever runs Daimon for the server can. The exception is bounded:

- only a direct @mention, never a plain message or a reply without one;
- at most one reaction per message, and at most one notice per person per
  channel per `DAIMON_DISCORD__PROTECTED_CHANNEL_NOTICE_COOLDOWN_MIN` (10
  minutes);
- no text is ever posted in the channel, and no turn runs;
- the DM is sent only if the person can receive DMs; when it fails, nothing
  else happens;
- only when a stored rule matches the channel or its resolved category. A
  refusal that comes from failing closed (a category that couldn't be looked
  up, or a policy that couldn't be read) stays silent, as do mentions replayed
  at startup.

A channel's **own agents** are those whose agent rule names it alone, while
its readers are `own`: that channel is their **home**. `own` goes on both
fields, or on `readers` with `writers: none`. A caller with no agent (the
CLI, an operator token) may still post where `writers` is `own`: that is
input, and the readers rule keeps it inside.

A rule sits on a channel or a Discord category, and a thread follows both; a
category only takes `writers: none`. Chat and the Permissions screens set
channel rules only: a thread id names its channel. The operator can still keep
one Slack or Discord thread to turns inside it (`daimon channels rule set
--thread ID --readers inside`); a thread takes the strictest of that, its
channel's and its category's rule.

## What a channel rule limits

| Limit | `readers: inside` | `readers: own` | `writers: none` | Model |
| --- | --- | --- | --- | --- |
| Turns start | yes | own agents only | no | `run_refusal` |
| Agents that answer and post | any | own | none | `run_refusal`, `post_refusal` |
| Read from outside (channel, sessions, history) | no | no | yes | `readable_from`, `session_readable_from` |
| Sessions read and continued by | turns inside | own agents inside | anyone allowed | `session_homes` |
| Memory writable | no | own agents only | yes | `memory_writable` |
| Content kept inside | no | yes | no | `keeps_content`, `held_to` |
| People from another organisation answered | no | yes | no | `answers_external` |
| Agents listed there | all | own only | all | `listed_at` |
| Default agent | any | own only; never cleared | any | `binding_refusal`, `clear_refusal` |
| Charged to its budget | its turns | its turns and its own agents' calls | its turns | `budget_channel` |

**Content kept inside** means a call from a turn in the channel, whatever
agent runs it, reads only the channel and the conversations that ran there,
posts only inside it, sends no direct messages and creates no agents. A call
by the channel's own agent is held the same way, wherever it runs. Routines
saved there post only there and never DM their results
(`keeps_routine_inside`). Only the channel's setup thread may still
configure its own agent.

**Publishing** (a report, notebook or upload URL) puts content behind a link
whoever holds it opens. From a channel whose content is kept inside, or by an
agent with a rule, it waits for the requester to press Approve on a card.
Agent keys and runs nobody watches can't publish there, and nobody changes
Daimon's server-wide name or avatar there. See [publishing](#publishing).

**Listed** covers agents, their skills, environments, routines, timers and
defaults. From outside, a channel's own agents are hidden everywhere.

**People from another organisation** (a Teams guest the tenant doesn't list
as a member) are answered only in a channel with `readers: own` or its
threads, never in its setup thread or a DM.

A thread whose channel is unknown fails closed while any channel's readers
are `own`, since it may lie in one (`home_unknown`).

## Agent rules

`runs_in` lists the only channels an agent runs in, threads included. It is
unset for an agent that runs wherever the [agent cascade](architecture.md)
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

## Changing agent setup

Server or workspace admins may change an agent's setup. A non-admin must
administer every channel named by the agent's rule, and every place the agent
answers or runs must stay inside those channels. This applies to instructions,
model, description, tools, skills and MCP server attachments, including token
forms and OAuth connections. Unbound agents, tenant defaults and agents serving
other channels require a server or workspace admin. Creation metadata and a
channel default alone do not grant permission to edit setup. Defaults-managed
specs retain their existing protections, including for admins.

When approval cards are off, a skill add needs a preview followed by the
requester's next message of `yes` in that thread. Only one preview is active
per person and thread, across agents. A newer preview replaces the previous
one. The confirmation names the agent and skill; a replaced preview must be
previewed and confirmed again before it can be added.

## Publishing

A turn whose admission would refuse `PUBLISH` gets its session's publish
tools (`PUBLISH_TOOLS`) on `always_ask`, so the platform shows an Approve
card before the call runs. The card names the action and who may answer, shows
the consequence, and shows a few plain labelled inputs behind Details. It
never shows raw payloads or tool and server identifiers. Approve and Deny
only work for the requester. Each call gets its own card and confirmation
event, even when several calls pause together. The card expires after
ten minutes, and a stopped turn removes its buttons. The MCP server checks
for itself: it reads the live session (`_session_gate.session_asks_first`)
and only then authorizes
with `approved=True`. A session without that card, an agent key or a run
nobody watches is refused, so a plumbing failure refuses rather than
publishes.

## Exemptions

- **Server admins** are exempt where the output reaches them alone: their own
  DM and hub turns, and hub reads of sessions inside limited channels. They
  configure agents with rules from anywhere and see every agent listed.
  Agent rules and `readers: own` still hold for them in channels, threads,
  routines and handoffs. Nothing writes where `writers` is `none`.
- **Channel admins** of a `readers: own` channel have the same exemption for
  its own agents. They configure an agent whose rule names only channels
  they administer, and read those channels' sessions from the hub. In Discord
  and Slack chat, the agent is told when the requester administers the current
  channel, so it can try the requested setup tool. Each tool still checks the
  target agent's reach before changing instructions, skills or keys.
- A **setup thread** under a `readers: own` channel answers as the built-in
  agent.

Agent keys and tokens with no person behind them are never exempt.
[Trust model](architecture.md#trust-model) has the full table.

## Changing the rules

Rules are server or workspace admins' calls, never a channel admin's:

- chat: `set_channel_rule` and `set_agent_rule` ([MCP tools](mcp-tools.md));
- panels: the channel's **Permissions** screen, from Who answers where in the
  Discord and Slack setup panels, and the Channel settings dialog on Teams;
- CLI: `daimon channels rule set` and `daimon agents rule set`;
  `daimon channels answerable` checks the result (below).

Setting `readers: own` makes the channel's default agent its own (a custom
agent answering nowhere else), or copies an agent for it (`copy_from`).
Moving readers off `own` keeps its agents' rules, so they still run only
there, unless `release_agents` drops them. Every write goes through
`with_channel_rule` and `with_agent_rule`, which refuse a rule nothing would
enforce. `archive_channel_copy` retires a copy when its channel closes.

`daimon tenants access-policy rules PLATFORM WORKSPACE_ID [--json]` lists
every channel, category and agent rule, and each agent's home.

**Where Daimon may answer.** `daimon channels answerable discord GUILD_ID
[--expect FILE] [--json]` lists the server's channels where Daimon may answer
at all: every channel not under a `writers: none` rule, its own or its
category's, plus the parent channel of every thread bound to an agent
(setup or handoff, archived and locked threads included), whatever that
channel's rule. It reads Discord's channel list for the server with the bot
token, so a channel with no stored config counts too, and it writes nothing.
Use it when another bot shares the server and the two must never answer in
the same place: export the other bot's channel list to a file, one id per
line (`#` starts a comment), and pass it as `--expect`. The command prints
the ids Daimon may answer in that the file lacks, the ids the file lists
where Daimon may not answer, and every bound thread whose parent the file
does not list, and exits 1 on any difference. It reports threads only
through their bindings, and DMs not at all.

It fails closed: what it cannot tell is answerable or an error, never
silent. A channel the bot cannot view still counts by its rules alone,
because the view permission can be granted later without touching a rule.
A channel whose category is missing from the server's channel list counts
as answerable unless its own rule is `writers: none`, and is reported as an
error (exit 1). A server the bot cannot see, or a failed Discord call, is an
error and prints no set.

**Stored policy.** The policy row holds `channel_rules`, `category_rules` and
`agent_rules`. A row an older build wrote (lists of protected, sealed and
isolated channels and channel pins) reads as the same rules and is rewritten
in the new shape on its next change, so no migration runs.

The invoker allowlist, the Teams guests counted as members, read-only DM
memory, channel admins, environments, budgets and operator tokens are not
channel or agent rules. They say who may act and who may change the rules.
