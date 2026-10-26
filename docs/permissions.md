# Permissions

## Agent tool rules

A current human workspace admin can set `allow`, `ask` or `deny` for one
third-party MCP tool on one agent with `daimon agents tool-rules` or the
`set_agent_tool_rule` MCP tool. An agent-wide
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

Daimon's own tools take rules too, under the key `daimon-mcp/<tool>` (the
tool's own name, not `call_tool`). Two layers enforce them.

The MCP server enforces an agent-wide `deny`. When a chat or routine session of
that agent calls the tool, directly or through `call_tool`, the server refuses
it before the tool runs, whatever the rule's scope. An agent-wide `deny` on
`daimon-mcp/*` denies all of Daimon's tools for that agent, including tools
added later: the server refuses every call from the agent's session,
`search_tools` and `call_tool` included. Wildcards are recognised only for
`daimon-mcp`, and only as an agent-wide `deny`; `allow`, `ask` and
channel-scoped `*` rules, and `*` on other servers, do nothing. A rule must
name the agent's own Daimon endpoint; a server that merely carries the name
`daimon-mcp` at another URL is not matched. If the server cannot read the
rules, it refuses the call. Operator tokens and agent keys are not affected.

The turn driver applies `ask`, `deny` and channel rules, which the server cannot
(it has no card and does not know the channel). Most chat sessions reach Daimon's
tools through the `call_tool` proxy, so a session whose agent has any `ask` or
`deny` rule on a Daimon tool, agent-wide or in any channel, asks before every
`call_tool` call and before each tool a rule names; the driver then reads the
tool inside the call and the rule that fits the channel. `deny` refuses the
call, `ask` waits for a card in chat and refuses in a routine, and a call with
no rule or an `allow` rule runs with no card. An agent-wide `daimon-mcp/*`
`deny` also makes the driver refuse every proxied call, with the same message as
any other deny. An agent with no rule on a Daimon tool gets no extra lookup and
no new card.

The tool part of a `daimon-mcp/` rule's key is limited to letters, digits, `_`, `.` and `-`
(up to 128 characters), or `*` for the `daimon-mcp/*` deny-all. Other names are
refused when such a rule is saved (a third-party key keeps the plain `server/tool` check), and a stored row with one is ignored and logged
(`tool_safety.rule_name_skipped`), because the name becomes a tool setting in
the session.

A rule on a publish tool turns the publish card on for the agent everywhere. The
session asks on a tool named by a rule in every channel, so a rule on
`publish_report`, `create_notebook_upload_url` or `create_attachment_upload_url`,
in any one channel, makes that tool card in every channel and refuse in
routines, even for an agent that is otherwise allowed to publish. Channels the
rule does not name still run the tool once the person approves. (`add_skill`
with a reviewed preview and `request_agent_backend_switch` already card
wherever tool safety applies, so a rule on them adds nothing.) A tool that waits
for a person's Approve is called by its own name; sent through `call_tool` it is
refused at once and the model is told to call it directly.

Gemini and OpenAI cannot pause for a card. A turn for an agent on one of them is
refused when the agent has an `ask` rule or a channel-scoped `deny` on a Daimon
tool, the rules only the driver applies. An agent-wide `deny`, a tool's own or
`*`, is enforced by the server, so those agents keep working there. The person
sees the same "this agent can't run here" notice as for any backend the agent
can't use; the cause (`daimon_tool_rules`) is in the operator log under
`turn.backend_unsupported`. Nothing runs before the refusal.

With both settings on, a card for a third-party MCP write classified as needing
approval also has **Always allow**. A current workspace admin can press it to
save an agent-wide `allow` rule for chat.
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

Server or workspace admins may change an agent's setup, and so may the
agent's **owners**. The first owner is the person who created the agent, from
chat or the setup panel's New agent button, or who copied it with `fork_agent`;
a channel's own copy of an agent keeps that agent's owner. An admin can name
the primary owner with `daimon agents owner set`, and add or remove further
owners (co-owners) with `daimon agents owner add` and `daimon agents owner
remove`; `daimon agents owner show` lists them all. A co-owner can do
everything the primary owner can. Nobody from another organisation can be an
owner. Agents created before owners existed have none and stay admin-only.

An owner may also manage GitHub repos for their own agent only:

| | Admin | Owner of the agent | Anyone else |
| --- | --- | --- | --- |
| Connect repos to the agent (any repo their GitHub account shows them on the installation; the agent gets write access) | yes | yes | asks an admin |
| Remove any repo from the agent, including one an admin connected | yes | yes | no |
| Choose the agent's working repo | yes | yes | no |
| Connect a repo for every agent on the server | yes | no | no |
| Installation notices, the GitHub home, new-repo cards, the waiting list and request approvals | yes | no | no |
| Add or remove owners | yes | no | no |

Removing a repo from one agent leaves a repo connected for the whole server
connected for the other agents. An owner's Connect GitHub link is checked
again when the repos are saved: if the person has stopped owning the agent by
then, nothing is saved. A Connect GitHub button on a card in a thread shows
its link only to the person who asked, to an admin or to an owner of that
agent; anyone else is told whom to ask.

The owner may not change what the whole workspace shares: the provider
allowlist, channel budgets, server-wide GitHub repos, the retired pasted-token
repo and skill-repo binds, deleting a skill from the workspace, archiving or
copying the agent, and its tool rules stay with admins. A channel default
alone does not grant permission to edit setup. Defaults-managed specs retain
their existing protections, including for admins.

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

## Held turns

A turn is **held** to the channel it was asked in when the requester belongs
to another organisation than the one that added Daimon (a *client*), or when
the channel is shared with another organisation. A requester whose
organisation the platform could not determine is treated as a client for that
turn only; nothing about them is stored.

A held turn reads and posts only inside its channel and the threads under it.
It cannot send direct messages, save routines, create agents or start
workers, and publishing needs the requester's Approve. It reads only sessions
that ran inside the same channel. No admin exemption applies, whatever the
requester's stored role. A client also cannot change any agent, channel,
budget or rule, and cannot start a turn in a DM, hub, routine or agent chat;
Daimon answers them in the channel they share.

Admission records what the platform verified. A person verified as belonging
to another organisation is stored as a plain user and marked foreign; the mark
is never cleared, so they stay a client when a later lookup cannot say. A
channel found to be shared, by the platform's own signal or by a verified
client asking there, is remembered, so every later turn in it is held.

A held turn's MCP credential carries the hold itself, so the tools never rely
on what other turns are running. It sees a short list of tools (channel reads
and posts, timers, its own sessions, publishing), and each applies the hold
before any rule: channel lists, search hits and where-am-I name only the held
channel, timers and sessions are limited to it, and posting into the
requester's own DM is refused. A client's credential that carries no hold is
refused every channel tool, and a model-supplied `origin_context_id` can only
narrow what the credential allows, never widen it.

The hold is separate from agent rules: the default agent and an agent
bound to several channels still run, read and post inside a held channel.

## Exemptions

- **Server admins** are exempt where the output reaches them alone: their own
  DM and hub turns, and hub reads of sessions inside limited channels. They
  configure agents with rules from anywhere and see every agent listed.
  Agent rules and `readers: own` still hold for them in channels, threads,
  routines and handoffs. Nothing writes where `writers` is `none`.
- A **setup thread** under a `readers: own` channel answers as the built-in
  agent.

Agent keys and tokens with no person behind them are never exempt, and no
exemption applies to a held turn.
[Trust model](architecture.md#trust-model) has the full table.

## Changing the rules

Rules are server or workspace admins' calls, :

- chat: `set_channel_rule` and `set_agent_rule` ([MCP tools](mcp-tools.md));
- panels: the channel's **Permissions** screen, from Who answers where in the
  Discord and Slack setup panels, and the Channel settings dialog on Teams;
- CLI: `daimon channels rule set` and `daimon agents rule set`.

Setting `readers: own` makes the channel's default agent its own (a custom
agent answering nowhere else), or copies an agent for it (`copy_from`).
Moving readers off `own` keeps its agents' rules, so they still run only
there, unless `release_agents` drops them. Every write goes through
`with_channel_rule` and `with_agent_rule`, which refuse a rule nothing would
enforce. `archive_channel_copy` retires a copy when its channel closes.

`daimon tenants access-policy rules PLATFORM WORKSPACE_ID [--json]` lists
every channel, category and agent rule, and each agent's home.

**Stored policy.** The policy row holds `channel_rules`, `category_rules` and
`agent_rules`. A row an older build wrote (lists of protected, sealed and
isolated channels and channel pins) reads as the same rules and is rewritten
in the new shape on its next change, so no migration runs.

The invoker allowlist, read-only DM
memory, environments, budgets and operator tokens are not
channel or agent rules. They say who may act and who may change the rules.
