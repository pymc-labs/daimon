# Permissions

## Approval gate overrides

Approval gate overrides require live platform-admin authority. Delegated admins
cannot change them; they are outside the delegated action allow-list.

Server or workspace admins can change whether attended chat asks before a
third-party MCP write, at server, channel or thread scope. Values are `on`
(ask), `off` (allow without a card), and `inherit`. The most specific explicit
value wins: a thread can turn asking back on beneath a server set to `off`,
and can turn it off beneath a channel set to `on`. There is no category scope.
A thread's requester has no special permission to change this setting.

Overrides are inactive by default. The operator must enable both
`DAIMON_TOOL_SAFETY__ENABLED` and `DAIMON_TOOL_SAFETY__ALLOW_GATE_OVERRIDES`.
Settings can be saved while inactive; the controls report that they are
stored but ignored. Every change is audited, and each write allowed by an
`off` setting logs the applied scope and the administrator who set it.

Only third-party `write_needs_confirmation` decisions can be relaxed. Deny
rules still deny, explicit agent `ask` rules still ask, and failed rule reads
still require confirmation. Names in the confirmed Daimon and publishing sets,
admin-confirmed actions, and gate or permission setters never receive an
approval override, whatever server exposes them. This includes watch actions,
skill addition, lead resolution, backend switches, Microsoft sends/writes,
publishing and channel/agent permission changes. With overrides enabled, an
unrelated third-party tool with a protected name conservatively asks too.
A second hostname, HTTP URL,
IP address or proxy cannot turn those names into ordinary third-party writes.
Endpoint-binding checks also retain fixed gates for recognized own-endpoint
aliases; attaching such aliases is refused.

Settings can only be changed by humans through the native admin controls on
`/here` (Teams: `here`). There is no CLI command or MCP tool for changing them.
Each click and modal submission checks current native server/workspace admin
authority; channel ownership, thread ownership, agent credentials and cached
admin claims confer no authority.

- **Discord:** use `/here` in a server channel or thread. Select **Server**,
  **Channel**, or **Thread** and choose ask, allow writes, or inherit. A thread
  card offers all three scopes, with Channel referring to its parent.
- **Slack:** use `/here` in the channel. Server and Channel choices apply
  directly. A Thread choice opens a native modal; paste the root message link
  from that channel or its timestamp. Daimon checks admin authority again and
  verifies the root in that workspace and channel before saving.
- **Teams:** type `here` in a channel post. The card offers Server, Channel
  and Thread controls, each with ask, allow writes and inherit.

The card displays the effective choice and its source. An inherited server
default asks; disabled deployment overrides are displayed as stored but ignored.
Direct messages do not consume any server, channel or thread override, including
a server set to `off`; their third-party writes keep their existing confirmation
behavior. Routines, wakes and other unattended runs still use `unattended_writes`.

The effective setting is read again before every prospective automatic allowance,
including in prepared and running turns. Switching `off` to `on`, or clearing it
to an asking parent, applies to the next write. An unreadable setting requires
confirmation. A send prepared with a stale looser gate is refused; retrying
requires a fresh admission, rather than repeatedly sending the stale grant.
The generic `tenants access-policy set --clear` refuses while approval overrides
remain; clear them using the native admin controls first.

Before rolling back to an older build, keep this build running while a native
server/workspace admin opens `/here` (Teams: `here`) and chooses **Inherit** for
Server and for every channel/thread with a saved setting. Alternatively, restore
a policy backup from before approval overrides were saved. Older policy readers
reject unknown fields, so stored `approvals` and `approvals_set_by` fields make
the policy unreadable and refuse
that tenant's turns. Disabling deployment overrides alone does not remove those
stored fields.


## Who is a Daimon admin

Daimon has one admin role per server or workspace, shared by all its agents
and channels. Platform admins always have this role, checked live: Discord
owners and members with Administrator or Manage Server, Slack workspace
admins and owners, and Teams owners of the configured admin team. Their
platform authority cannot be revoked inside Daimon.

Granted admins can change what agents do, including agents platform admins use.
Their changes to instructions, skills, environments and tool rules also affect
platform admins’ routines and attended chats that use those agents.

Platform admins can grant Daimon admin to a user ID, or on Discord to a role
ID. A role grant applies while the member holds that role. Delegated admins
can administer only the actions in `DelegatedAction` in
`packages/core/daimon/core/admin_actions.py`:

- `agent_settings`: agent instructions, identity, backend and settings.
- `channel_permissions`: channel/agent rules and participation settings.
- `routines`: routine configuration and management.
- `skills`: agent/channel skills and skill environments’ attachments.
- `environments`: agent/channel environments.
- `model`: model selection.
- `routing`: who answers where, channel defaults and handoff configuration.
- `tool_rules`: agent/channel tool rules and their approval panels.
- `agent_lifecycle`: agent copies and archiving.
- `hub_pin_exemption`: the hub’s agent-pin exemption when starting a turn.

Every other admin action is platform-only, including credentials, third-party
MCP connections, credential-based skill-repository setup, coding-tool access
and DM enable/disable. Long-job policy, worker spending limits, provider
allow-lists and channel provider-pin clearing stay platform-only. Worker
spending-limit tools require a personal CLI credential; chat execution cannot
change them. Long jobs retain their explicit human command-approval card and
owner permissions; their admin management exemption remains platform-only.
GitHub connect,
request approval and repository binding require platform admins at both the
adapter and store boundaries. Delegates do not get the hub exception for
reading other people’s sessions or sealed channel transcripts; that exception
uses only the stored platform-admin role. The hub pin exemption above grants
no transcript access. Agent ownership is separate from delegation: the owner
permissions below still apply to that person’s own agent, and a grant never
confers ownership. GitHub and money controls require platform authority even
for owners.

Authority changes and all money controls require a human acting directly:
a platform panel/button rechecks live platform authority, and MCP requires a
registered personal CLI credential (`token_kind=cli`) re-attested live as a platform admin.
Chat execution (even an admin’s attended chat), routines, continuations,
watches and external agent keys cannot grant/revoke admins, read or change
billing/budgets/caps, redeem promos or create checkout sessions. Operator
tokens cannot exercise these controls either. Deployment-wide promo issuing
remains available through the local operator CLI; MCP issuing is refused.
Delegated admin is never cached in account roles or turn origins. The model
receives `effective_role` separately from the platform `current_role`; these
controls describe the current turn and grant no additional authority.

Routine execution authority follows the author of its behavior. If another
person changes a routine’s message, agent or target, the routine transfers to
that editor and uses their identity for future runs. The transfer is audited
atomically, old output and pending delivery are cleared, and private delivery
follows the new author. Pause, resume and delete do not transfer authority.

Account roles are written only by turn admission and the platform-checked
`/github connect` commands. Setup notices, GitHub link helpers and operator-token
panels never promote accounts. Operator-token minting requires an existing
platform-admin snapshot as well as the live check. If the snapshot is missing,
Daimon refuses before creating a token and asks the platform admin to send
a message in the server/workspace and retry. Slack hides GitHub/operator-token controls from
non-platform admins and checks platform authority inside removal notices.

Legacy tokens with no registered kind and short-lived billing-panel tokens
are not personal credentials. Billing-panel tokens serve only the checkout
HTTP hop after a live-checked button; they cannot grant/revoke or call the
MCP money tools. Checkout re-attests the bound account as a platform admin.

Open **Who answers where → Permissions → Admins** in Discord, or
**Who answers where → Admins** in Slack. Each change checks platform
authority again and records who changed which grant and when. Revocation
takes effect on the next click or MCP request, including already-open
panels and existing credentials. Platform authority and any remaining grants
still apply. Erasing a user removes their grants and scrubs their attribution.

MCP provides `list_admins` for members and `grant_admin` / `revoke_admin` for
platform admins acting directly with registered personal CLI credentials
(`token_kind=cli`). It rechecks platform authority live; every agent-run credential and operator
token is refused. Teams uses MCP or the CLI.
The CLI commands `daimon admins list`, `daimon admins grant USER_ID`, and
`daimon admins revoke USER_ID` call these same authenticated tools. Set
`DAIMON_ADMINS_URL` to the MCP endpoint and `DAIMON_ADMINS_TOKEN` to your
platform-user MCP credential. Add `--role` for a Discord role ID. Local
operator context or a supplied actor ID does not prove platform authority.

## Agent tool rules

A current human Daimon admin can set `allow`, `ask` or `deny` for one
third-party MCP tool on one agent with `daimon agents tool-rules` or the
`set_agent_tool_rule` MCP tool. An agent-wide
rule applies where the agent runs; a channel rule applies in its parent
channel and threads under it. When both match, `deny` wins over `ask`, which
wins over `allow`. The key is exactly `server/tool`, and a server name
never contains `/`: attaching one is refused, and a call on a server
attached with one before that asks in chat and is refused unattended,
whatever its keys or rules say. A rule is
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

The rule set is read when a turn starts and again whenever the turn's admission
is re-decided (a session built or reused, a fleet worker prepared), so a rule
added or cleared reaches the driver at once. The session's own tool settings
follow it at the next bind. A rule never loosens a gate Daimon already has:
`add_skill` with a reviewed preview and the publish tools of an agent that asks
before publishing still wait for their card, and the operator's deny list still
comes first. An agent `deny` wins over those cards. These cards do not offer
**Always allow**.

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

An agent whose Daimon toolset turns its default off and enables tools by name
sees exactly those tools, each listed under its own name. Managed Agents only
exposes the names a toolset enables, and Daimon otherwise lists most of its
tools only behind `search_tools` and `call_tool`, so the server reads the
agent's list (recorded whenever a session for it is created or has its tools
replaced) and lists those tools directly for that agent's chat credential. The
two proxies appear only when the list names them.

Gemini and OpenAI cannot pause for a card. A turn for an agent on one of them is
refused when the agent has an `ask` rule or a channel-scoped `deny` on a Daimon
tool, the rules only the driver applies. An agent-wide `deny`, a tool's own or
`*`, is enforced by the server, so those agents keep working there. The person
sees the same "this agent can't run here" notice as for any backend the agent
can't use; the cause (`daimon_tool_rules`) is in the operator log under
`turn.backend_unsupported`. Nothing runs before the refusal.

With both settings on, a card for a third-party MCP write classified as needing
approval also has **Always allow**. A current Daimon admin can press it to
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

## Agent-chat turns

Native OpenAI session credentials carry an isolated session grant installed
during preparation. Preparation alone grants no origin authority: the driver
binds the grant to the current execution and its attendance only after acquiring
the conversation lease, and revokes that binding before releasing the lease.
Queued turns have no execution grant; stopping, cancelling or expiring a waiter
does not change the running turn’s grant or attendance. Reuse binds a new origin; child sessions receive
a separate grant and cannot borrow a parent execution.

Agent chat and the hub can drive only sessions created through agent chat.
They cannot continue, cancel or archive a platform conversation, even for the
same account and agent. Older sessions without this creation provenance must
be replaced with a new agent-chat conversation. Transcript read access is unchanged.

A turn started through agent chat (`start_turn`, `continue_turn` and `ask`,
with an agent key or on the hub) has nobody to press Approve, so it runs
under the same tool-safety rules as a routine: reads run, a third-party write
is refused unless `DAIMON_TOOL_SAFETY__UNATTENDED_WRITES` lists it (or an
agent `allow` rule with `chat_and_routines` covers it), and `deny` always
refuses. Agent tool rules for the turn's channel apply as well as agent-wide
ones: the channel of a key minted in a channel, otherwise the channel the
conversation belongs to (a Slack thread's parent channel; never the source
channel of a DM). A routine's private transcript stamp keeps its channel
rules when the session is continued through the CLI, MCP or a checkpoint.
Before every message the session's tool settings are set
again, so every MCP call asks, whatever the session was created with and with
tool safety on or off. The process that
sent the turn answers its tool calls, deciding each one when it is made.
With `DAIMON_TOOL_SAFETY__ENABLED` off, ordinary third-party calls are
allowed.

Daimon's own actions that wait for a person's card in chat ask in these
sessions and are refused there. Switching an agent's backend and sending
Microsoft mail or invites are refused with tool safety on or off; Microsoft
file changes, which ask in chat only with tool safety on, are refused only
with it on.

## What an unattended session may carry

Every turn nobody watches on Managed Agents builds its session through one
helper, `daimon.core.turn.unattended_capabilities`, before its message and
before each confirmation batch. (A turn on another backend has no such
session to check; it refuses every tool call that pauses.) The helper refuses
the turn, before anything is sent or changed, when the session:

- belongs to an agent that coordinates other agents (`multiagent`). The
  roster's agents carry their own tools and MCP servers, which the gate cannot
  rewrite, and Managed Agents cannot remove a roster from a live session or
  override it when one is created.
- carries an unreviewed field in a capability-bearing part: the
  agent container, resources (files, repositories and their checkouts, memory
  stores), execution identity, skills, MCP servers, tools, toolset defaults,
  per-tool settings and permission policies. Unknown fields are refused even
  when empty or newly parsed by an SDK update. The refusal names the path and
  explains that the capability behavior has not been reviewed. New top-level
  session fields are ignored and their names logged once per process; known
  resource, vault, environment and agent fields retain their capability checks.
  Memory-store access accepts only `read_only`, `read_write` or the provider's
  omitted/null default. Non-capability parts are opaque: usage and all its sub-objects,
  stats, budget, outcome evaluations, metadata and `agent.model` have neither
  their keys nor their values checked. Capability fields retain their reviewed
  value shapes; unknown tool, resource, skill, identity and permission-policy
  types are refused. One walker checks
  SDK model fields and response extras or raw provider objects. Metadata and
  custom tool argument schemas remain opaque, and SDK private instance
  attributes are not checked.
- carries skills on an entry point that does not allow them.
- has an MCP server whose name contains `/`. Tool keys join a server and a
  tool with `/`, so such a name could stand for a tool on another server.
- runs on a Managed Agents runtime that cannot read the live session, so the
  checks above cannot be made. Nothing is sent, not even the first message.

Every MCP toolset the session keeps then asks on every call, on its default
and on each per-tool setting: Daimon's own tools, and third-party tools with
tool safety off, included. Each call therefore reaches the unattended decider,
which answers as described above, with the agent's and the channel's rules:
Daimon's ordinary tools and, with tool safety off, ordinary third-party calls
are still allowed. Built-in sandbox tools (bash, file
reads and edits, web fetch and the rest) are disabled for every unattended
entry point, and custom tools are removed. The one exception is a scheduled
routine that an admin has opted in:

| Entry point | Built-in tools | Skills |
| --- | --- | --- |
| Watches | off, no opt-in | denied |
| Scheduled routines | off, unless an admin opted that routine in | allowed |
| Timers and worker-finished wakes | off, no opt-in | allowed |
| Fleet workers | off, no opt-in | allowed |
| Agent chat | off, no opt-in | allowed |
| `daimon run` | off, no opt-in | allowed |
| Workspace checkpoints | off, no opt-in | allowed |

With built-ins off, the session's built-in toolset is
disabled (every tool `enabled: false`) and the decider refuses any built-in
call that still pauses. An opted-in routine keeps its built-in toolset as the
agent defines it; custom tools are still removed.

### Routine sandbox opt-in

The operator CLI, `daimon routines sandbox-tools`, is the supported way to opt
a routine in today. The `set_routine_sandbox_tools` MCP tool accepts only a
live Discord Daimon admin's own token, re-checked against the server on every
call; no Daimon surface issues such a token yet, and the tool is kept for a
future admin surface. It refuses agent keys, agents acting in a conversation
on an admin's behalf, operator tokens, legacy tokens without a token id, and
Slack or Teams callers, and owning the routine is not enough. DM routines
cannot be opted in.

An opt-in covers exactly what was approved: built-ins are enabled for a run
only when the run's agent version and environment are exactly the ones
recorded at opt-in, and any mismatch or missing record keeps them off. An edit
to the agent or its environment by anyone allowed to configure it therefore
turns the opt-in off at the next run, as does an edit to the routine's agent,
trigger message, destination or budget channel. Every change is recorded with
who made it, when, the routine id and the agent version and environment it
covers (`routine_sandbox_history`). Routine status shows the current setting.
The record covers the Managed Agents agent version and environment only, not
Daimon-side attachments (the agent's `.env`, repository binding, vault
credentials or channel skills); an admin should re-review an opted-in routine
after changing those.
The opt-in applies to that routine's own scheduled runs only. See
[routines.md](routines.md#sandbox-tools).

A refused routine records `AdmissionDenied: backend_unsupported` as its
result. Agent chat answers with a tool error, `daimon run` exits with the
same error, and a workspace checkpoint carries only the conversation to the
new session.

In-process unattended runs leave pending calls paused and interrupt the session
when a decision is cancelled, its turn ceiling expires, or its confirmation
gate fails. Stop, cancellation and ceiling cleanup send no confirmations, including
on runtimes without a confirmation gate. A runtime other than Managed Agents
without a confirmation gate still refuses unattended confirmations during a
normal, uninterrupted decision. Cancellation during decision-task
or card cleanup also finishes bounded interrupt cleanup before propagating.

Native OpenAI workers and unattended wakes cannot use a profile that needs
automatic sandbox, bash, custom tools, or other capabilities without host
inspection. Daimon refuses before sending a message to the provider. An
operator's approval of a native profile does not opt it into unattended
sandbox execution. Conversation-only native workers and attended native
conversations remain supported. Selfhosted runs inspect every tool through
the host's current policy, so allowed reads can continue while unattended
sandbox calls and unapproved writes are refused.

Reading a session never answers a tool call. If the service restarts while an
agent-chat turn waits on one, the turn stays paused, and `get_session` and
`get_my_session` report `status: paused` with the hint "paused: call
continue_turn to resume". Only the session's own account can resume it, by
calling `continue_turn`, with or without a message; the waiting calls are then
decided by the policy in force at that moment, under the channel rules of the
message that started the turn, and the session is checked and its tools set
again before any new message. An admin who can read another member's channel
conversation on the hub cannot resume it. A turn that agent chat did not
start, such as a chat turn waiting on someone's confirmation card, reads as
paused with the hint "paused: waiting for approval where it was asked", and
`continue_turn` refuses it: only that card answers it. The list tools report
the provider's own status, so a paused turn lists as `idle`.

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

Daimon admins may change an agent's setup, and so may the
agent's **owner**. The owner is the person who created the agent, from chat
or the setup panel's New agent button, or who copied it with `fork_agent`; a
channel's own copy of an agent keeps that agent's owner. An admin can also
name one with `daimon agents owner set`. An agent has at most one owner.
Agents created before owners existed have none and stay admin-only.

The owner may change the agent's instructions, model, description, tools,
skills, MCP server attachments (including token forms and OAuth connections),
keys, model provider and its required capabilities, from chat and
from the Discord, Slack and Teams setup panels. They see their agent's setup
(its prompt and connections) and find it with `list_agents` (`mine=true`), even
while it answers nowhere. An owner reaches their own agents past channel
isolation too, including a channel's copy they own, since the copy keeps its
source's owner. Because the owner can rewrite the prompt, keys an admin added
to that agent are not secret from its owner: the agent can be told to use or
reveal them. Give an agent's keys only to an agent whose owner may hold them.
A person from another organisation is never an agent's owner. Making the agent share threads stays with admins,
because it applies in every channel the agent answers in that doesn't pin a
backend; the owner may keep an admin's choice or return to one thread per
person. If two people create agents of the same name at once, one is refused;
nobody becomes the owner of an agent someone else created. Ownership is read
again when a change is written, when a form is submitted and when a
confirmation card is clicked, always for the person acting, so a change of
owner applies at once. An agent key or a requester from another organisation
is never the owner.

Worker spending limits require a live platform admin acting directly with a
personal CLI credential, including on an agent the caller owns.

The owner may not change what the whole workspace shares: the provider
allowlist, channel budgets, GitHub access (repo and skill-repo binds, grants),
deleting a skill from the workspace, archiving or copying the agent, and its
tool rules stay with admins. A channel default alone does not grant
permission to edit setup. Defaults-managed specs retain their existing
protections, including for admins.

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

- **Platform admins** retain the hub exception for reading sessions inside
  limited channels, including other users’ sealed transcripts. Delegated
  admins have only the live hub agent-pin exemption; their transcript reads
  follow ordinary user access. Daimon admins configure agents with rules
  from anywhere and see every agent listed.
  Discord cold-DM admission and all private Discord delivery enforce agent
  pins and isolation even for admins. Agent rules and `readers: own` still
  hold for them in channels, threads,
  routines and handoffs. Nothing writes where `writers` is `none`.
- A **setup thread** under a `readers: own` channel answers as the built-in
  agent.

Agent keys and tokens with no person behind them are never exempt, and no
exemption applies to a held turn.
[Trust model](architecture.md#trust-model) has the full table.

## Changing the rules

Rules are Daimon admins' calls, :

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
at all: every channel where the turn gate would let a turn start, that is one
not under a `writers: none` rule, its own or its category's. It reads
Discord's channel list for the server with the bot token, so a channel with
no stored config counts too, and it writes nothing. Agent rules (`--runs-in`,
`--anywhere`, `--nowhere`) are not considered: they choose which agent
answers, not whether Daimon may.

It also lists every thread bound to an agent (setup or handoff, archived and
locked threads included) and whether it answers. A bound thread passes the
same gate, so under a `writers: none` channel it is refused like the channel
and adds nothing to the set.

Use `--expect` when another bot shares the server and hands some channels
to Daimon. The file lists the channels that bot does not answer, one id per
line (`#` starts a comment); Daimon's answerable set must equal it exactly.
The command prints the ids Daimon may answer in that the file lacks, the ids
the file lists where Daimon may not answer, and every answering bound thread
whose parent the file does not list; any difference, either way, exits 1.
It reports threads only through their bindings, and DMs not at all.

It fails closed: what it cannot tell is answerable or an error, never
silent. A channel the bot cannot view still counts by its rules alone,
because the view permission can be granted later without touching a rule.
A channel whose category is missing from the server's channel list counts
as answerable unless its own rule is `writers: none`, and is reported as an
error (exit 1). A thread binding whose parent channel is not in the list is
reported as an error and not counted. A server the bot cannot see, or a failed Discord call, is an
error and prints no set.

**Stored policy.** The policy row holds `channel_rules`, `category_rules` and
`agent_rules`. A row an older build wrote (lists of protected, sealed and
isolated channels and channel pins) reads as the same rules and is rewritten
in the new shape on its next change, so no migration runs.

The invoker allowlist, read-only DM
memory, environments, budgets and operator tokens are not
channel or agent rules. They say who may act and who may change the rules.

Origin-based MCP actions, including task handoffs, require the explicit
`origin_context_id` and the calling session's server-issued grant. Knowing a
live origin UUID, even for the same account and agent, grants no authority.
Empty IDs never discover a concurrent human turn. A handoff retains attendance
only after that session check and only when its stored origin is attended;
routines and timer turns cannot acquire human approval surfaces this way.
Legacy sessions using a shared caller credential are replaced at their next
bind. Personal OAuth connections remain in the caller's shared vault.

## Discord invoker roles

A tenant's durable access policy can contain `invoker_role_ids`. A current
server admin (when `invoker_admin_bypass` is true), a user listed in
`invoker_user_ids`, or a verified member with any listed role may start turns.
The user and role lists form a union. Both lists empty means open. Writer and agent rules still apply independently.
Private guild-bound access also requires current server membership. Role
matching uses IDs, so renaming a role does not change access.

Set roles with repeatable `daimon tenants access-policy set discord GUILD_ID
--invoker-role ROLE_ID`. This replaces the role list; `--clear-invoker-roles`
clears only that list. `access-policy get` and its `--json` output show IDs.
IDs must be Discord snowflakes; the guild's own ID (`@everyone`) is refused.
Slack and Teams reject nonempty role lists, including invalid stored policies;
their user allowlists retain their existing behaviour.

Ordinary checks cache positive membership for at most 30 seconds and absence
for five seconds. Member updates/removals invalidate gateway evidence
immediately. Without member events, role revocation can take up to 30 seconds
to affect an ordinary cached check. Lookup failures never extend a grant;
expired evidence fails closed. Private result delivery requires a live lookup, including Discord MCP replies
and transcript reads.


### Per-agent invoker roles

An operator can restrict an individual agent to humans who currently hold at
least one specified Discord role. The human must also pass the tenant's
invoker policy. Server admins must hold a matching role too; the tenant's
`invoker_admin_bypass` does not exempt them from an agent's role requirement.

```sh
daimon agents rule invoker-roles discord GUILD_ID AGENT_NAME --role ROLE_ID --role OTHER_ROLE_ID
daimon agents rule invoker-roles discord GUILD_ID AGENT_NAME --clear
daimon tenants access-policy rules discord GUILD_ID
```

The policy stores these roles as `agent_rules.AGENT_NAME.invoker_role_ids`.
Role requirements are independent of `runs_in`; changing where an agent runs
preserves its invoker roles. Each restriction on any of the agent's names must
be satisfied. Empty roles leave the agent's existing admission behavior intact.
Role IDs must be Discord snowflakes and cannot name `@everyone` (the guild ID).

Membership is checked live when the target agent is selected, on continuations
and queued sends, and before model sends and tool confirmations. Revocation
refuses work at the next such check; it does not interrupt a step already in
progress. An unavailable membership lookup also refuses further work through
the usual invoker refusal.
Handoffs check the person requesting the switch against the destination agent.
Workers spawned by a human-requested turn inherit its human principal, even
though workers have no confirmation cards. Both the root agent's and worker
target's role requirements are checked against fresh membership at dispatch.
API/MCP chat also enforces the destination's requirement.
Routines, scheduler jobs, bots and other non-human invocations cannot use an
agent with this requirement. Saving a routine for a role-gated agent is refused.
Watches, timers, long jobs and fleet completion wakes are unattended and cannot
invoke these agents. A handoff from an unattended turn cannot switch to one,
even when its recorded owner currently holds a matching role. Unknown origins
also carry no human principal and are refused. GitHub-access-ready and
private-input-applied continuations are refused too, even though their clicks
retain attended tool controls for ungated agents. Role-gated agents do not
receive their own `worker_finished` completion wakes.

Handing a thread away from a role-gated agent skips the old agent's workspace
checkpoint. The conversation transcript crosses with an explicit gap for the
working files; the predecessor receives no checkpoint send.

Archiving an agent drops its role rule. Setting invoker roles on one of an
agent's aliases replaces the invoker roles on its other aliases, preserving
spatial rules on every alias.

Roles are Discord-only: writers refuse setting this rule on Slack, Teams or CLI tenants, and unsupported membership fails closed.


### Discord cold direct messages

Cold DMs require a ready, unarchived tenant with DMs enabled and an explicit
`dm_agent_name`. Membership is mandatory even for an explicitly allowed user ID;
unknown membership is never a grant. Discovery and selector clicks require fresh
membership through the shared resolver, then check the invoker policy, configured
recipient restrictions and the tenant-owned agent. Pinned agents and agents held
inside an isolated channel cannot run in a cold DM, including for server admins.
The live admin bypass for the invoker allowlist still applies.

No eligible tenant, revoked membership or role access, disabled DMs and denied
recipient access produce no model turn, with a structured reason log. A cold DM
with no eligible tenant gets a one-time reply per person per process: a verified
member refused by the invoker policy or an agent's role requirement is told they
lack permission to DM Daimon, and anyone else gets the `/dm-me` guidance, which
names no server; revoked access to an existing conversation suppresses its reply.
Revocation during a running turn suppresses its private result. The active server
is retained; sending another DM never falls through into a different tenant.
An unknown membership lookup during discovery can ask for a retry without naming
servers. Server selection belongs only to the requester and their physical DM route,
expires after five minutes, and rechecks access before creating a scope.

Cold conversations copy no public history and grant no additional channel access.
They mount the tenant's and caller's personal memory under ordinary private-turn
rules. `dm_memory_read_only` keeps its false default; enabling it makes those mounts
read-only, including when changed between admission and session preparation.
Explicit server switching starts empty private history in a new scope and retires
the old session mapping. Routine notifications do not select a tenant or become
history in the active DM conversation. `/dm-me` remains the explicit channel-context
move, with its source sealing and budget checks.


Private Discord delivery checks the answering agent's retained admission grant
against the current `RUN_AGENT` and `DIRECT_MESSAGE` rules before every output
chunk, with no admin exemption for agent pins or isolation. This applies to moved
conversations too, whose source sealing remains independently enforced. Archiving,
deleting or transferring the bound agent to another tenant, or an unreadable
private access policy, suppresses replies without choosing a different tenant.
Billing and operational notices remain separate from these silent access denials.

DM routines are scheduled owner messages that run through normal live-DM
admission, binding and execution, unattended and on Managed Agents only. Each
run rechecks current membership, the invoker role policy and its admin bypass
switch, tenant DM controls, recipient policy, agent access and creation
provenance. Protected actions requiring a card are refused, and nobody watches
intermediate actions. [Routines](routines.md) has the full
rules, including source provenance and what an edit erases.


The platform-neutral `invoker_admin_bypass` defaults to true, including for old
policies. Set `--no-invoker-admin-bypass` to require admins on Discord, Slack,
and Teams to match the same invoker lists as other members; restore the pass
with `--invoker-admin-bypass`. Both empty lists still mean open. This does not
change writer rules or agent pin/isolation admin exemptions. Text and JSON
`access-policy get` output include the switch.

## Lead intake

[Lead intake](lead-intake.md) requires live tenant membership, the configured
agent and ordinary invoker admission on every call, plus current native sales
channel access and destination/category/agent policy before effects. The sales
channel must restrict the default guild role. Manual sources require the
requester's own explicit registration; isolated source data cannot be exported.
Only configured user mentions are enabled, with no role or everyone pings.

Resolution requires a configured sales resolver or current core-role membership,
with immutable completed identities and an encrypted evidence audit. Server
admin status alone is insufficient. `invoker_admin_bypass` governs ordinary
admission; disabling it gives admins no exception to the core-role/user gate.
Agent keys and operator tokens cannot impersonate an interactive resolver.
Routine calls require the exact configured routine, tenant, executing agent,
creator and a still-live server-issued execution grant. Routines never resolve.

Lead resolution also requires a live interactive session that held the exact
resolver tool for the requester's approval. Tenant opt-in adds that native
approval gate even with third-party tool safety off; a role grant or a routine
allowlist alone cannot approve selections.

Lead intake rechecks the actual dossier thread, its parent and category before
recovery and each write. Thread creation requires the caller’s native create
and thread-send permissions.

Reading a lead follows its destination's history. A record keeps the sales
channel it was captured for; every reader (MCP tools, `/leads`, an approval
card's Details) needs native view and history on that channel and on its
dossier thread, private-thread membership where it applies, and the channel
rules for both, judged with the executing agent and its origin, so an agent
kept to its own channel reads no lead elsewhere. Reconfiguring intake never
widens who reads past records. Unclassified discoveries hold no message content
and are read under the channel configured now. List results omit inaccessible
records. Read-only tools do not require send permission.

Lead tools use only the origin bound to the caller’s signed session grant and
require attended provenance for human actions. Concurrent conversations of the
same user and agent cannot lend their channel read or approval authority.

### Channels visible outside the organisation

Discord and Slack show a warning in `/here` and the current channel's Permissions
screen when people outside the organisation can view a channel whose readers are
not **Only its own agents** (`own`). Admins can click **Make confidential** to set
readers to `own` through the existing channel-rule controls. Posting becomes
`own` unless replies are already off (`none`). The click checks admin access
again. This controls which agents can read the content; platform membership and
channel visibility still belong to Discord or Slack.

On Discord, admins name the organisation's internal roles using **Name your
internal roles** on the Permissions screen. Any one of these roles counts as
internal. Without a selection, the panel asks you to name them and does not claim
to have checked outsider visibility. Server admins use this in-product Permissions
panel. The CLI is a **deployment-operator** interface: possession of the deployment's
DB/config grants policy access; it does not check a Discord identity or platform-admin
attestation. This applies to every access-policy option, including `--internal-role`,
`--clear-internal-roles`, and the general `--clear` (which removes internal roles and
the rest of the policy). Operators can replace or clear the selection:

```sh
daimon tenants access-policy set discord GUILD_ID --internal-role ROLE_ID --internal-role OTHER_ROLE_ID
daimon tenants access-policy set discord GUILD_ID --clear-internal-roles
```

The Discord check uses current role permissions and channel overwrites, including
category-synced permissions. Non-internal roles with access trigger a warning.
Member overwrites granting access are checked with individual member lookups,
without the privileged members intent. More than 100 such overwrites, or a failed
lookup, conservatively triggers a warning.

On Slack, `/here` and the current channel's Permissions screen each use one fresh
`conversations.info` response. External sharing, sharing across workspaces within
an Enterprise organisation, and pending sharing trigger the warning. The check
uses Slack's sharing flags (`is_shared`, `is_ext_shared`, `is_org_shared`,
`is_ext_ws_shared`, `is_pending_ext_shared`, `is_global_shared` and org-wide channel
flags), plus workspace-ID lists showing current or pending sharing with another
workspace. A list containing only this workspace does not by itself trigger a
warning. See Slack's [conversation fields](https://docs.slack.dev/reference/objects/conversation-object/)
and [channel fields](https://docs.slack.dev/reference/objects/channel-object/).

Nothing is cached, and no member or workspace directory is read for this check.
When readers is not `own`, the screen always says: **Guest accounts aren't checked
on Slack; if this channel has guests, make it confidential.** An unshared channel
is not certified free of outsiders. Failed or incomplete sharing lookups show
**Outsider check skipped** as well. If later Slack metadata lookups fail, `/here`
still shows known outsider evidence against the current reading rule, with no admin
actions or unrelated channel details. If the bot cannot see the channel, `/here`
asks you to invite it and says that the outsider check was skipped.
Teams does not use this check.
