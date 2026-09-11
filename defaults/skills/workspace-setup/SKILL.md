---
name: workspace-setup
description: First-time workspace setup and agent-roster operations for a Daimon workspace — working repo, keys, skills, MCP servers, routines, and which changes need an admin.
---

# workspace-setup

## Setting up a workspace for the first time

For an open-ended setup request, establish what the person wants the agent to
do, then configure only what that needs. A specific request such as “add a
Higgsfield key so people here can try it” is already a complete setup intent;
do not turn it into a full setup interview.

Choose the target before changing anything: an explicitly named agent wins,
then a selected setup target if the context actually supplies one, then the
answering agent in ordinary chat. Daimon being the responder does not replace
an explicit research-bot target. If the target is missing, deleted, or still
ambiguous, ask one concise question instead of silently choosing another.
State the target before a consequential change. Selecting a target does not
change which agent answers the conversation.

1. **Working repo.** Use `request_repo_binding` for the requested GitHub repo.
   Collect tokens only through its private form. A GitHub App install link is
   informational: installing alone does not verify this workspace's access or
   bind the repo. Use the working token path where access is needed; do not
   say an existing token stopped being used after an App install.
2. **Keys.** Use `request_agent_key` for an API key the agent will use in code,
   including a key for an unfamiliar or newly launched service. Infer and state
   a conventional name such as `HIGGSFIELD_API_KEY`; do not ask the person to
   design a variable name. A key can be added to Daimon itself without a fork,
   an MCP server, or a skill. Accept it before researching how to use the API;
   consult the service's documentation when a later task needs that knowledge.
3. **Skills.** Use `list_skills` to find existing skills and `update_agent` to
   attach them to an editable agent. An admin can import a GitHub skill bundle
   from chat with `sync_skills`. If it needs a private token, use
   `request_skill_repo_token`: submission imports and attaches the skills and
   also binds the target's working repo so subsequent imports can find the
   token. Explain that repo change before requesting it.
4. **MCP servers.** Use `attach_mcp_server` for a server needing no token, or
   `request_mcp_token` for a supported connection that needs one. An API key
   for code is not automatically an MCP connection token. Ask “API access for
   code or an MCP connection?” only when the request leaves that choice unclear.
   A token form does not complete an arbitrary browser OAuth login.
5. **Confirm the result.** Explain what changed and what still needs doing.
   Read partial-success warnings: a saved token with a failed attachment is
   not a connected server, and an imported skill is not necessarily attached.

## Roster operations

Use `list_agents` and `get_agent` to find and inspect agents. `create_agent`
creates one; `fork_agent` makes an editable copy, including a copy of Daimon.
Use `update_agent` for Prompt & model or skill additions. Use `remove_skill`
and `detach_mcp_server` for removal, rather than replacing lists through
`update_agent`. Removing a skill from one agent is different from
`delete_skill`, which deletes it from the workspace library. `archive_agent`
and `delete_skill` are admin-only.

When creating an agent and no model was requested, use the built-in Daimon's
model, read through `get_agent`, as the fallback and state that choice.
Confirm the new name and model, carry its returned identity into subsequent
setup, and say whether it answers anywhere. Creation alone does not route
mentions to the new agent.

## Permissions and refusals

An admin means Manage Server on Discord or a workspace admin on Slack. Read
`is_admin` on the newest `<user_query>` and check the requested operation's
rule. A member can ask setup questions, create an agent, and contribute a new
key to Daimon. Do not turn non-admin status into a blanket setup refusal.
For replacing a shared key, hand the request to an admin rather than attempting
the replacement on a member's behalf.

An agent answering in a channel or as the workspace default is admin-managed
for direct prompt, model, skill, and MCP-spec edits. Direct edits to the built-in
Daimon's spec require an editable copy even for admins. Offer `fork_agent` for
those edits; keys, working-repo binding, and posted-token operations follow
their own rules and must not get a blanket fork requirement.

When the operation needs an admin and the caller is not one, do not attempt
it. Give a reachable handoff carrying the target and action, for example:
“Ask an admin to say in this conversation: ‘Make research-bot answer in this
channel.’” If no agent answers in the current channel, name the existing
`/agent-setup` entry rather than telling the person to talk to an unreachable
agent. Do not invent panel paths or a new setup entry.

An operator-only problem needs the person running the deployment, not a
workspace admin. Name the blocker and the requested fix without exposing
internal exceptions. Never report a failed save as successful or erase the
successful half of a partial result.

## Keys and tokens stay in private forms

Use `request_agent_key`, `request_mcp_token`, `request_skill_repo_token`, or
`request_repo_binding` in the current conversation. These tools collect no
secret value in their arguments; the requester enters it in a private form.
The request expires, but a saved key does not expire with the request.
State the shared-use consequence once: “Anyone who talks to research-bot can
use it.” Do not force a separate setup conversation for a key request.

If someone pastes a value in chat, acknowledge the exposure and ask them to
rotate it. Never repeat the value or pass it to any tool. Post the appropriate
private form for the replacement; do not claim the model never saw the pasted
value or that the bot removed it from history.

Stored keys and available session resources are different facts.
`list_agent_keys` describes the target's stored names, never values, and does
not prove the responder can use them. `remove_agent_key` removes a stored key
from the target. Inspect the mounted `.env` by name only when needed, following
the key-handling preamble. Do not claim a save refreshed an existing session
or tested the service. If multiple available keys plausibly fit the task,
ask one concise question.

After a confirmed save, continue the original task when the needed resources
are actually available, or give a concrete supported next request. Do not
duplicate a confirmation card in prose, automatically charge a paid service
for a test, or offer an unrelated skill-authoring project.

## Which agent answers where

There is one bot account per deployment, not one per agent. A mention resolves
the channel override, then the workspace default, then the deployment default.
Use `explain_agent_resolution` to inspect that choice.

Admins use `set_agent_default` and `clear_agent_default` to change who answers:
with `channel_id` they affect that channel; without it they affect the workspace
default. Clearing an override exposes the next tier, which may still resolve
to the same agent. Say what scope will change before calling the tool. Do not
promise a running conversation will switch responders or continue automatically.

## Following threads

Replying in a thread nobody addressed you in is off by default, and staying
off is the preferred answer — turn a thread on when someone asks you to follow
it, and off again when they ask you to stop.

Two tools, scoped by which id is given:

```
set_thread_participation(mode, thread_id=..., channel_id=...)  # this thread (both ids)
set_thread_participation(mode, channel_id=...)                 # this channel
set_thread_participation(mode)                                 # the whole workspace
get_thread_participation(thread_id=..., channel_id=...)        # who decided, and what
```

Modes are `on`, `off`, `disabled` (channel and workspace only: off, and no
narrower scope may override it), and `inherit` (drop this scope's own setting).

Resolution cascades deployment default (the operator's environment), then
workspace, then channel, then thread: the narrowest scope with a setting wins,
except that a `disabled` tier beats every tier below it. Any member may set a
thread; channel and workspace scopes require a workspace admin (Manage
Server), and a non-admin caller is refused with no write. Discord only for now.

## Scheduled routines

Use `create_routine` after confirming the agent, schedule, timezone, and task.
Report the actual creation result and any remaining setup needed for the task.

## Live configuration and repository defaults

The live tools change deployed resources. Repository defaults are operator-owned
seeded specifications: the next defaults apply reconciles managed resources
for existing tenants as well as new installs. User-owned forks are separate.
Edit repository defaults only when asked to change those deployment defaults
or open a PR for them; they are not a substitute for a workspace setup tool.
