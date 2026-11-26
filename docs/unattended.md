# Unattended turns

Some turns run with nobody there to press Approve. This page says which turns
those are, how their tool calls are decided and what their sessions may
carry. [Permissions](permissions.md) covers the rules an attended chat turn
follows.

## Which turns are unattended

- Scheduled routines, including routines that report by DM.
- Watch reports.
- Timers, long-job completion wakes and handoffs those wakes create.
- Agent chat: `start_turn`, `continue_turn` and `ask`, with an agent key or on
  the hub.
- `daimon run` and workspace checkpoint turns.

A turn is attended only when a real person's message or button click started
it. A queued handoff keeps the attendance of whatever started it, so a timer
cannot gain an approval card by handing off.

## How tool calls are decided

Every MCP call in an unattended session pauses for one decider, Daimon's own
tools included. With
[`DAIMON_TOOL_SAFETY__ENABLED`](configuration.md#daimon_tool_safety__enabled)
off, ordinary calls are allowed. With it on:

- reads from attached MCP servers run;
- a third-party write is refused, and the agent told why, unless
  [`DAIMON_TOOL_SAFETY__UNATTENDED_WRITES`](configuration.md#daimon_tool_safety__unattended_writes)
  lists it or an agent `allow` rule with `chat_and_routines` covers it;
- a `deny` rule always refuses, and an `ask` rule refuses.

Actions that wait for a person's card in chat are refused, whatever the
setting. Switching an agent's backend and sending Microsoft mail or invites
are refused with tool safety on or off. Microsoft file changes are refused
with it on. Lead resolution is always refused.

Agent tool rules for the turn's channel apply as well as agent-wide ones. For
agent chat that is the channel a key was minted in, else the conversation's
channel, never the source channel of a DM.

Stopping or cancelling an unattended turn, or reaching its ceiling, leaves
pending calls paused and interrupts the session. It never sends an answer,
since even a denial would resume the run.

## What a session may carry

Before the message and before every batch of confirmations, Daimon reads the
live session and sets its tools from the current policy. Each send is one
attempt. The turn is refused before anything is sent when the session:

- belongs to an agent that coordinates other agents (`multiagent`), whose
  roster's tools the gate cannot reach;
- carries a field this release has not reviewed in any part that grants a
  capability, from resources down to a tool's permission policy;
- carries skills on an entry point that does not allow them;
- has an MCP server whose name contains `/`;
- runs on a Managed Agents runtime that cannot read its session.

A refused routine records `AdmissionDenied: backend_unsupported`. Agent chat
answers with a tool error, `daimon run` exits with that error, and a
checkpoint carries only the conversation.

Built-in sandbox tools (bash, file reads and edits, web fetch and the rest)
are off, and custom tools removed, for every unattended entry point:

| Entry point | Built-in tools | Skills |
| --- | --- | --- |
| Watches | off | denied |
| Scheduled routines | off, unless an operator opted the routine in | allowed |
| Timers and wakes | off | allowed |
| Agent chat | off | allowed |
| `daimon run` | off | allowed |
| Workspace checkpoints | off | allowed |

A [long job](long-jobs.md) is the one exception: its own isolated worker has
built-in tools and nothing else.

## Opt a routine into sandbox tools

An operator can turn built-in tools back on for one channel, thread or
destination-free routine, for its own scheduled runs:

```bash
daimon routines sandbox-tools --tenant TENANT_UUID ROUTINE_ID [ROUTINE_ID ...] --dry-run
daimon routines sandbox-tools --tenant TENANT_UUID ROUTINE_ID [ROUTINE_ID ...]
daimon routines sandbox-tools --tenant TENANT_UUID ROUTINE_ID --off
daimon routines sandbox-tools --tenant TENANT_UUID ROUTINE_ID --inherit
```

`--off` keeps built-ins off for the routine even when its agent is opted in.
`--inherit` clears the routine's own setting, so its agent's applies.

The opt-in records the provider, agent version and environment the routine
runs with. A run gets built-ins only while all three still match. After anyone
edits the agent or its environment, or moves it to another provider, the next
run goes without them and the opt-in is cleared until the operator runs the
command again.

Editing the routine's agent, trigger message, destination or budget channel
also clears it. Changing the schedule, pausing or resuming does not. DM
routines cannot be opted in individually, only through their agent, and
owning a routine is not enough.

### Per agent

An operator can instead opt in an agent, which covers every routine run of
that agent, DM routines included:

```bash
daimon routines sandbox-tools --tenant TENANT_UUID --agent AGENT_NAME [--environment NAME] --dry-run
daimon routines sandbox-tools --tenant TENANT_UUID --agent AGENT_NAME
daimon routines sandbox-tools --tenant TENANT_UUID --agent AGENT_NAME --off
```

This records the agent and the environment's name (the workspace default
unless `--environment` is given), not a version, so editing the agent keeps
the built-ins. Another agent or environment still runs without them. A change
of agent provider clears it, and the routine opt-ins under the old agent.
A routine's own setting wins over it, and a workspace lock wins over both.
The `unattended_capabilities` setting still wins: with it off, built-ins stay
off. Turn it on only for agents whose runs you trust.

The record follows the agent id and the environment's name, so an
environment recreated under the same name keeps the built-ins. `--off`
takes the agent by name or, for one renamed or gone, `--agent-id`;
`daimon routines sandbox-tools-list --tenant TENANT_UUID` shows every agent
and routine opted in. The `/routines` panel says when the agent holds the opt-in.

The record does not cover what Daimon attaches to a run, such as the agent's
keys, repository or channel skills. Review an opted-in routine after changing
those. The setting is `routine_sandbox_tools` in the [features](features.md)
table, and each change shows in the settings history with what it covers.
`get_routine`, `list_routines` and the Discord
`/routines` panel show whether a routine is opted in.

<!-- knowledge: web -->
The web app's Routines page shows it too.
<!-- /knowledge -->

The `set_routine_sandbox_tools` MCP tool accepts only a live Discord server
admin's own token, which no Daimon surface issues yet.

## Agent chat sessions

Agent chat and the hub drive only sessions that agent chat created. They
cannot continue, cancel or archive a platform conversation, even for the same
account and agent.

To wait for a turn, call `await_turn`. It returns when the agent says something,
when the turn ends, or after about four minutes, so the caller polls nothing.
One call per return carries a long job to its end.

Reading a session with `get_session` or `list_events` never answers a tool
call. If the service restarts while an agent-chat turn waits on one,
`get_session` reports `paused` with the hint "paused: call continue_turn to
resume". `await_turn` and `continue_turn` resume it, for the session's own
account only. The waiting calls are then decided by the policy in force at that
moment. A turn that reaches the [unattended run time limit](features.md) pauses
the same way, and resumes the same way.

A chat turn waiting on someone's card reads as "paused: waiting for approval
where it was asked", and `continue_turn` refuses it. The list tools report the
provider's own status, so a paused turn lists as `idle`.

## Other providers

A turn on a backend other than Managed Agents has no session to check, so it
refuses every tool call that pauses. With the `host_call_gate` deployment
feature on, an OpenAI or Gemini turn nobody watches keeps its Daimon tools:
each call gets the unattended verdict above. DM routines and DM timers run
there too, as their owner's private turn, and channel timers as unattended
replies. Before each send Daimon rechecks the source's seal and the owner's
roles. Watch reports run only on Anthropic. Automatic custom tools are always
refused before the first message.

OpenAI's persistent workspace always has a shell, so its unattended turns run
only where sandbox tools are allowed, such as a long job. A timer has no
sandbox opt-in, so an OpenAI timer is refused. With the feature off,
native turns that need MCP, sandbox, bash or custom tools are refused before
the first message. A self-hosted harness inspects every tool at the host, so
allowed reads continue while sandbox calls and unapproved writes are refused.

## Reads from a signed server

A [signed server](mcp-servers.md#let-a-server-verify-who-is-calling) decides what
each call may read from the claims Daimon signs. Server settings say which turns
with no new message it can recognise:

| Turn | Signed as | Setting |
| --- | --- | --- |
| A routine that posts to a channel or thread | that routine and room | `routine_claims` |
| A Managed Agents routine that posts to its owner's DM, while its run is live | that routine, the owner and their DM, room `dm` | `routine_claims` and `dm_routine_claims` |
| A timer, watch report, finished long job, hand-off or resumed form that runs in its own thread | its reason, read as a routine posting there | `wake_claims` |
| A person's turn, on each call while it runs | `wake: turn`, beside the person and message | `wake_claims` |

Other turns sign none of these claims, so the settings do not change what they read.
These include a routine with no destination or on OpenAI or Gemini, a timer in a DM or
channel, and a watch report to a DM or a new thread. Watch polls, helpers, agent chat, `daimon run` and checkpoints
are the same.

### A routine in its owner's DM

A routine that posts to its owner's DM signs `routine`, the owner as `sub`, the owner's
DM as `channel` and `room: dm`, with no `msg`. Daimon opens that DM before the run,
sending nothing. It signs only while the call comes from that routine's live run, and the
routine is enabled, unedited, still posting to that owner's DM and running this agent.
A pause or an edit removes these claims from the next call, which is then signed as
without the setting. An agent that does not mount `daimon-mcp` never signs them.

Switching `dm_routine_claims` on lets a stored prompt read for its owner with nobody
watching, as a fresh DM from the owner would, without writes. The model or an admin may
have written that prompt; the answer still goes only to the owner's DM.

| `routine_claims` | `dm_routine_claims` | Routine runs the server can tell apart |
| --- | --- | --- |
| off | off | none |
| on | off | runs posting to a channel or thread |
| on | on | those, and runs posting to the owner's DM |
| off | on | refused in configuration |

Update the server first, then all Daimon processes, then turn `dm_routine_claims` on.
Turn it off before rolling either back, and let running DM routines finish first.

## Origins

A tool that acts on a conversation needs its `origin_context_id` and the
calling session's own signed grant. Knowing another live origin's id, even for
the same account and agent, grants nothing. An empty id never finds a running
turn. Each session gets its grant when its turn starts and loses it when the
turn ends.
