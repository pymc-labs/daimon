# Helpers and handoffs

How agents work together: an agent asks helpers inside one turn, a program asks an
agent from outside chat, and a person hands a thread to another agent. Every path
keeps the requester's rules, budget and approvals. For the single-agent picture, start
with [How Daimon works](how-daimon-works.md).

## Helper agents

An agent has a **roster**: the other agents it may ask for help during a turn. The
agent calls three tools, which work on every model provider:

| Tool | What it does |
| --- | --- |
| `list_helpers` | Lists the agents it may ask, with id, name and purpose. |
| `ask_helper` | Gives one helper a task and returns its reply. |
| `wait_for_helpers` | Waits for replies, for about a minute at a time. |

Admins set a roster from chat with `set_agent_helpers`, behind a Confirm card.

<!-- knowledge: web -->
An admin can also pick helpers on the agent's canvas in the web app.
<!-- /knowledge -->

A helper runs for the same person, in the same channel and thread, as if that person
had asked it there. Its tools, keys, rules, budget and approval cards all apply. It
keeps its own conversation per thread and posts nothing: its last message is the
reply to the asking agent, and the person never sees it.

## Run helpers in parallel

Pass `wait=false` to `ask_helper` to start a helper and ask others before waiting. A
roster entry of `self` gives a fresh copy of the asking agent for each ask, so one
agent can fan out. Copies share no files with it. Files go both ways by link: the agent
uploads with `create_file_upload_url`, and a helper's files return as download links
that expire after an hour.

Limits keep a turn bounded:

- Three helpers run at once, and one turn makes at most 20 asks.
- Helpers stop when the asking turn ends, so the agent waits before it answers.
- A helper asks no helpers of its own.
- Copies of self share a $10 budget per turn.

Only a person's own message or a handoff starts a turn that may serve helpers. A
routine, a watch or a relay turn asks none. Approvals for a helper's writes follow
the asking agent's own value ([Approvals](approvals.md#approvals-per-agent)).

## Agent-to-agent requests

A program, or another agent, can ask a Daimon agent outside chat with an **agent
key**. The agent-chat tools `ask`, `start_turn` and `continue_turn` start or continue
a conversation, and the key holder sees only those tools. The turn runs
[unattended](unattended.md#which-turns-are-unattended): a write that needs a card is
refused, and `unattended_time_limit` bounds the run.

A key acts as the person who made it, with that person's budget and rules. See
[Permissions](permissions.md#use-coding-keys).

## Handoffs

A person can move a thread to another agent without opening a new thread. They ask
the agent, which calls `hand_off_task`, or press **Switch to agent** when a channel's
agent changes. The new agent answers at once when the person's own message asked for
it, and otherwise at the next message.

The destination must be allowed in that channel, and channel admins hand threads only to
agents of theirs. The conversation and files carry over while the agent stays on its
provider. After a provider move, the new agent gets a summary instead. The new agent
answers everyone in the thread from then on. See
[Architecture](architecture.md#same-thread-handoff).

## Turn origins

Every turn has an **origin** that says what started it: `chat`, `routine`, `relay`,
`handoff` or `helper`. Daimon adds a short instruction for the origin before the
person's message. A helper is told another agent is asking and that its last message
is the reply.

An agent may replace or extend the instruction for each origin with
`context_fragments`; see
[Defaults](defaults.md#add-invocation-context-fragments). A tool that acts on a
conversation also needs the turn's own signed grant, so knowing another turn's
id grants nothing ([Unattended turns](unattended.md#origins)).
