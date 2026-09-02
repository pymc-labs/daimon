---
name: workspace-setup
description: First-time workspace setup and agent-roster operations for a daimon workspace — repo binding, skills, MCP servers, credentials, routines, and what's admin-managed versus open to build.
---

# workspace-setup

## Setting up a workspace for the first time

When someone asks to configure this agent or get a workspace started ("help
me set up", "configure me", "get me started"), walk through the following, in
order, and confirm the shape with the user before you consider it done:

1. **Repository.** Ask which GitHub repository they want the agent working
   against, then call
   `request_repo_binding(agent_name, repo_url, purpose, channel_id)` — never
   accept a GitHub token in chat (see "Credentials never travel through
   chat" below). This posts a button naming the exact repo and agent; the
   user completes the bind by clicking it, which opens a private form
   asking for the branch and, only if the repo is private, a GitHub token.
   They only need to paste a token if the repo isn't publicly readable.
2. **Skills.** Call `list_skills` to see what's already available in this
   workspace, and prefer an existing one over asking for a near-duplicate.
   Attaching an existing skill to an agent works via
   `update_agent(name, skills=[...])`. Bringing a brand-new skill bundle into
   the workspace from a GitHub repository is an admin action, done from
   `/agent-setup`'s Skills door — not from chat.
3. **Additional MCP servers.** For a server that needs no auth token, collect
   a name and URL, confirm with the user, then call
   `attach_mcp_server(agent_name, server_name, url)`. For one that needs a
   token, never accept it in chat — call
   `request_mcp_credential(agent_name, server_name, url, channel_id)` instead
   (see "Credentials never travel through chat" below).
4. **Confirm the final shape** — repository, skills, MCP servers — with the
   user before calling it done.

If the agent doing the configuring is the one this deployment ships with,
steps 2 and 3 can't target it directly — it isn't editable at all (see "When
an operation is refused"). Offer to `fork_agent` it into an editable copy
first, then run these steps against the fork.

## Roster operations

| Tool | What it does |
|---|---|
| `list_agents` | List every agent in this workspace. |
| `get_agent` | Get one agent's full configuration by name. |
| `create_agent` | Create a new agent from scratch. |
| `fork_agent` | Clone an existing agent under a new name — the way to get an editable copy of an agent you can't edit directly. |
| `update_agent` | Patch-update an agent's model, prompt, tools, skills, or MCP servers. Omitted fields are left alone; passing `[]` for a list field clears it. |
| `attach_mcp_server` | Attach a new MCP server to an agent. |
| `detach_mcp_server` | Remove an attached MCP server from an agent, and its matching tool entry, together. |
| `remove_skill` | Detach one skill from one agent. |
| `list_env_credential_keys` | List the environment-variable KEY NAMES set on an agent — never values. |
| `remove_env_credential` | Remove one environment variable from an agent. |
| `create_routine` | Schedule a recurring turn (see "Scheduled routines" below). |

`remove_skill` only detaches this one agent's reference to a skill — the
skill itself, and every other agent using it, are untouched. That is a
DIFFERENT operation from **delete_skill**, which deletes the skill for the
entire workspace; don't reach for it when a user just means "stop this agent
using X."

## When an operation is refused

Anyone can build and configure their own agent. But an agent that is
currently set as the default for a channel or for the whole workspace is
admin-managed — changing its prompt, model, skills, or MCP servers needs a
workspace admin, because people are already relying on that configuration.
And the agent this deployment ships with can't be edited at all, by anyone,
admin included — fork it (`fork_agent`) to get an editable copy, and
configure that instead. Deleting an agent (**archive_agent**) is admin-only
in the same way — a workspace admin can archive from chat; for anyone else
it goes through `/agent-setup` or the `daimon agents archive` CLI command.

## Credentials never travel through chat

`request_env_credential`, `request_mcp_credential`, and
`request_repo_binding` post a single-use, expiring button in the thread;
clicking it opens a private modal where the user enters the value, so it
never enters channel history or the session log. They work the same way on
Discord and Slack, so on either platform post the button rather than
sending the user to `/agent-setup`.

If a user pastes a token or secret in chat anyway: acknowledge that you saw
it, call no tool with it, and tell them to rotate it immediately — it is
already in this channel's history and in the tenant-wide session log,
neither of which the bot can scrub. Then post the right button so the
replacement is entered privately. Once a credential is added via either
tool, it becomes usable by everyone who talks to that agent — say so when
you post the button.

## Which agent answers where

Members reach an agent only by @mentioning the bot in the channel they're
posting in — there is one bot for the whole workspace, not one bot per agent,
so no configuration step ever adds a new bot to the server. What a mention
resolves to — which agent answers — is controlled by the binding path below,
not by anything the member does.

Two admin-only tools control that binding, scoped by whether a channel is given:

```
set_agent_default(agent_name, channel_id)   # channel_id given -> this channel only
set_agent_default(agent_name)               # channel_id omitted -> workspace-wide default
clear_agent_default(channel_id)             # clears one channel's override
clear_agent_default()                       # clears the workspace-wide default
```

Resolution cascades channel override first, then the workspace default — a
channel with its own override always wins over the workspace default, and
clearing a channel's override falls it back to whatever the workspace
default resolves to next. Both tools require a workspace admin (Manage
Server); a non-admin caller is refused with no write. The equivalent surface
without chat is the setup panel's **Set as default…** door, which writes the
same channel/workspace scopes and shows the same cascade.

## Scheduled routines

The agent creates routines itself, with
`create_routine(agent_name, cron_expr, timezone, trigger_message)`. Confirm
the agent, the cron expression, the timezone, and the trigger message back to
the user before calling it, and confirm the created routine back afterward.

## Changing a running resource vs. changing the repo defaults

The live tools above (`update_agent`, `attach_mcp_server`, and the rest)
change the deployed resource the user is talking to, right now. Editing
`defaults/*.yaml` in the repository only changes what a fresh install seeds
going forward — it does not change any agent that is already running. Only
touch the YAML when a user explicitly asks to change the repo's seed
defaults or open a PR against them.
