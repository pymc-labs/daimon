# How Daimon works

The concepts behind Daimon, in the order a new reader needs them. Each section
says what the thing is and links to the page that covers it in full.

## Workspace

A **workspace** is one Discord server, Slack workspace or Microsoft 365
organisation. Agents, channels, keys, memory, credit and settings all belong to
one workspace, and one workspace never reads another's. People in it are members
or **admins**. See [Isolation](isolation.md) for what each scope owns and
[Admins](admins.md) for who counts as an admin.

## Agents and the roster

An **agent** is a model with a prompt, tools, skills and connected servers. A
workspace is seeded with a `default` agent, and admins add or fork more. Each agent has
**owners** who may change its setup, and a **roster** of other agents it may ask
for help.

An agent runs on one model provider: Claude, OpenAI, Gemini or a self-hosted
harness. The provider decides some limits. See [Defaults](defaults.md) for the seeded
agents, [Model providers](providers.md) for the backends and
[Permissions](permissions.md#change-agent-setup) for who may change an agent.

## Channels, threads and direct messages

Mention an agent in a channel and it answers in a **thread**. A thread is one
conversation with one running session, so people in the thread share its files and
history. A **channel rule** can limit who reads and writes in a channel, and an
**agent rule** can keep an agent to some channels.

A **direct message** (DM) is a private conversation between one person and an agent.
It has its own session, and an admin decides who may DM which agent. See
[Permissions](permissions.md), [Direct messages](direct-messages.md),
[Slack](slack.md) and [Microsoft Teams](teams.md).

## Skills

A **skill** is a folder of instructions and files the agent reads when a task
matches. Skills come from the seeded defaults, a library, a GitHub repo an agent
follows, a connected server or a channel. Admins and owners attach them to an agent;
a channel can add its own.

`library_skills` serves `list_library_skills` and `read_library_skill` to coding keys, the hub and
other clients. A scoped skill, in `list_skills`, `get_skill` or on an agent or channel,
reaches only an audience holding its scope: everyone in the room, or in a direct message the
asker and the agent's owner. On an agent or channel, Slack and Teams rooms never load it.
A declared `skill_repos` entry adds a repository's skills to the library with its scope
([Data sources](data-sources.md#declared-config)).

See [Defaults](defaults.md#skills), [GitHub](github.md) and
[MCP servers](mcp-servers.md#add-a-skill-a-server-serves).

## Memory

Memory is what an agent keeps between conversations. **Shared** memory belongs to
the agent, and everyone who talks to it can read it. **Personal** memory belongs to one
person and agent, and only that person's sessions mount it. A restricted channel
keeps **channel** memory of its own.

**Repository memory** holds notes about a GitHub repo that every agent working on
it shares. See [Isolation](isolation.md#agent-memory) and
[Repository memory](repo-memory.md).

## Keys and MCP servers

A **key** is a secret an agent uses for a service, such as a token for a data API.
Agent keys are shared by everyone who uses the agent, and a member may add one but
only an admin may replace it. A **coding key** lets Claude Code talk to one agent.
It reads data as its maker, keeping its channel's rule. The hub lists data tools only
where `data_reads` is On.

An **MCP server** gives an agent more tools, such as Notion or Linear. A server
that signs people in lets each person connect their own account. See
[Isolation](isolation.md#keys-and-credentials), [MCP servers](mcp-servers.md),
[Permissions](permissions.md#use-coding-keys) and the [Claude Code plugin](plugin.md).

## Routines, timers and watches

A **routine** is a recurring task: a schedule, an agent and a prompt. It runs with
nobody watching and may post its result to a channel, thread or DM. A **timer** fires
once, and a **watch** polls a source and wakes the agent only when it changes.

Both run as an [unattended turn](unattended.md). See [Routines](routines.md) and
[Watches](watches.md).

## Helper agents and handoffs

An agent may ask the agents on its roster for help inside one turn, and a person can
ask an agent to hand a thread to another agent. Agents can also be asked from outside
chat, with an agent key. See [Helpers and handoffs](helpers.md).

## Approvals and permissions

Before an agent writes to an outside service in a chat reply, Daimon posts an
**Approve** card, and only the requester can answer it. Admins choose where agents
ask and where they may write without asking. **Permissions** are the rules for who may
read, write and change what. See [Approvals](approvals.md) and
[Permissions](permissions.md).

## Features and their levels

A **feature** is anything a person or operator may want on or off, such as watches,
long jobs or direct messages. Each is set at one or more **levels**: deployment,
workspace, agent, channel and thread. A level set to Default inherits from the level
above, and an admin may lock a value so lower levels cannot change it.

Set features with `/settings` in chat, the `change_setting` tool or the CLI:

```bash
daimon features set watches off --tier workspace
```

See [Features](features.md) for every feature, its levels and its page.

<!-- knowledge: web -->
The web app also sets features, in its Settings section.
<!-- /knowledge -->

## Knows Daimon

**Knows Daimon** (`self_knowledge`) lets an agent answer questions about Daimon
itself. It gives the agent four read-only tools, `read_daimon_docs`,
`search_daimon_docs`, `read_daimon_source` and `search_daimon_source`, that serve these
pages and the source of the running release. The agent then answers "how does Daimon
work" and "how do I do this" from the release it runs on, not from memory.

It is on by default and can be set at any level. An agent's owner or an admin turns it off
for that agent in its settings, with `/settings`, or from the CLI:

```bash
daimon features set self_knowledge off --tier agent --agent NAME
```

<!-- knowledge: web -->
## The web app

The **web app** is a browser front end for the same agents. People chat with agents,
members manage their own routines, and admins manage people, channels, credit,
integrations and settings. It has its own sign-in and its own sections, which admins can
switch off per workspace. See [Web app](web.md).

An agent whose Web app feature is Off is not shown this section or any other text about
the web app.
<!-- /knowledge -->
