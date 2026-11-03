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
```

The opt-in records the agent version and environment the routine runs with.
A run gets built-ins only while both still match. After anyone edits the agent
or its environment, the next run goes without them and the opt-in is cleared
until the operator runs the command again.

Editing the routine's agent, trigger message, destination or budget channel
also clears it. Changing the schedule, pausing or resuming does not. DM
routines cannot be opted in, and owning a routine is not enough.

The record does not cover what Daimon attaches to a run, such as the agent's
keys, repository or channel skills. Review an opted-in routine after changing
those. Every change is recorded with who, when, the routine and the version it
covers. `get_routine`, `list_routines` and the Discord `/routines` panel show
the setting.

The `set_routine_sandbox_tools` MCP tool accepts only a live Discord server
admin's own token, which no Daimon surface issues yet.

## Agent chat sessions

Agent chat and the hub drive only sessions that agent chat created. They
cannot continue, cancel or archive a platform conversation, even for the same
account and agent.

Reading a session never answers a tool call. If the service restarts while an
agent-chat turn waits on one, `get_session` reports `paused` with the hint
"paused: call continue_turn to resume". Only the session's own account can
resume it. The waiting calls are then decided by the policy in force at that
moment.

A chat turn waiting on someone's card reads as "paused: waiting for approval
where it was asked", and `continue_turn` refuses it. The list tools report the
provider's own status, so a paused turn lists as `idle`.

## Other providers

A turn on a backend other than Managed Agents has no session to check, so it
refuses every tool call that pauses. Native OpenAI and Gemini profiles that
need automatic sandbox, bash or custom tools are refused before the first
message. Conversation-only workers and attended conversations still work. A
self-hosted harness inspects every tool at the host, so allowed reads continue
while sandbox calls and unapproved writes are refused.

## Origins

A tool that acts on a conversation needs its `origin_context_id` and the
calling session's own signed grant. Knowing another live origin's id, even for
the same account and agent, grants nothing. An empty id never finds a running
turn. Each session gets its grant when its turn starts and loses it when the
turn ends.
