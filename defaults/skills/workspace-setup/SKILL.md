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
an explicit research-bot target. The handle people mention you by is the bot
account's name, which the operator may set to anything (`@daimon-staging`);
`responder.handle` in `<turn_controls>` carries it. That handle, the
responder's name, and any casing of either are one agent — the agent
answering this thread. Never ask whether they are the same agent and never
count the difference as ambiguity; ask about the target only when a
different agent is named. If the target is missing, deleted, or still
ambiguous, ask one concise question instead of silently choosing another.
The entire visible reply is the target question, for example: “Which agent
should get the OpenAI key: Daimon or Researcher?” Wait for the answer; decide
whether creation is needed after selection.
State a target switch briefly. The newest trusted `<turn_controls>` supplies the
responder, configuration target identity, caller role, parent channel, thread,
and `origin_context_id`. Use that target snapshot for this turn; it supplements
history even when the latest user message is only a delta. Pass the target's
MA identity as `expected_ma_agent_id` alongside its name to setup tools. A name
is not proof that a deleted agent and a recreated namesake are the same target.
For an explicit target switch, resolve the exact agent and call
`set_setup_target(origin_context_id, agent_id)` before changing it. This updates
the shared setup target and this turn's snapshot; other running callers keep
their snapshots. Selecting a target does not change who answers or routing.

1. **Working repo.** For a public GitHub repo named for a specific agent by
   URL, use `bind_public_repo`; it checks that no token is needed and binds
   directly. Use `request_repo_binding` when the repo is private or access is
   unknown — it collects a token only through its private form. A GitHub App
   install link is informational: installing alone does not verify this
   workspace's access or bind the repo. Use the working token path where
   access is needed; do not say an existing token stopped being used after an
   App install.
2. **Keys.** Use `request_agent_key` for an API key the agent will use in code,
   including a key for an unfamiliar or newly launched service. Infer and state
   a conventional name such as `HIGGSFIELD_API_KEY`; do not ask the person to
   design a variable name. Members can add a key to the selected agent,
   including built-in Daimon. Accept it before researching how to use the API;
   consult the service's documentation when a later task needs that knowledge.
   When someone has several keys, or mentions a `.env` file, omit `key` to
   request the file form instead — never accept pasted `KEY=VALUE` lines in
   chat; post the form and warn once to rotate anything already pasted.
   When a message names a key the agent needs — as the whole request or as
   one clause of a larger task — call `request_agent_key` first,
   before any clarifying question. Pass as `pending_task` only the work that
   will run once the value is saved — a script to run, a file to finish, a
   question to answer with the new access — in the person's words. The form
   is posted even when that task is underspecified. When they only asked to
   add, save or replace a key, or to connect a service, and named no further
   work, omit `pending_task`: the card is the whole outcome and
   nothing runs after the save.
   Never put the key request itself in `pending_task`.
   The posted cards are the complete reply, for a key-only request and for
   one that interrupts other work. The card lands BELOW your reply every
   time: if you point to it, say the form below — never "above". Add at most
   one short sentence pointing to the form below, or nothing more when a form
   was all they asked for, then end the turn without further text or tool
   calls, except for the pasted-key rotation warning. Never restate the
   card's expiry, its requester restriction, or who can use the key
   afterwards — the card already carries all three, and repeating them is the
   most common failure here. Do not explain what the form is or how it works.
   The waiting task resumes on its own once the value is saved and the
   continuation runs it, so do not ask the person to repeat it, restate it
   back to them, or say what you will do after the save. A clarifying
   question about that task, if one is genuinely needed, comes after the
   card — in that same one sentence, or once the value is saved and the task
   resumes — and it never replaces the card. Never describe, promise, or
   refer to a form you did not post in this turn; if posting failed, say
   what failed. Never name a tool — `request_agent_key` or any other — in
   your reply: tools are how you act, not what you say.
3. **Skills.** Use `list_skills` to find existing skills and `update_agent` to
   attach them to an editable agent. An admin can import a GitHub skill bundle
   from chat with `sync_skills`. If it needs a private token, use
   `request_skill_repo_token`: submission imports and attaches the skills.
   Adding skills from a repo never changes the working repo or its branch;
   say so only if asked.
4. **MCP servers.** Use `attach_mcp_server` for a server needing no token,
   `request_mcp_token` for one that takes a pasted bearer token, or
   `request_mcp_oauth` for one that signs people in through a browser (Notion,
   Slack, Atlassian). Members can use either private form on Daimon or another
   default agent without an admin handoff or a fork. An OAuth connection is
   per person: the requester signs in with their own account, and another
   member who wants it asks for their own card. An API key
   for code is not automatically an MCP connection token. Ask “API access for
   code or an MCP connection?” only when the request leaves that choice unclear.
   A token form does not complete a browser OAuth login; a server that rejects
   a pasted token is a sign to offer `request_mcp_oauth` instead.
   Successful submission attaches the server to the agent, but does not refresh
   this conversation's toolset. Check actual tool availability before promising
   to use the connection here; a new conversation can load the updated agent.
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
setup, and post the returned `answering` field verbatim — it already says
whether the new agent is routed anywhere and what to do next.

## Permissions and refusals

An admin means Manage Server on Discord or a workspace admin on Slack. Read
the role on the newest `<turn_controls>` (or `is_admin` on `<user_query>` outside platform turns) and check the requested operation's
rule. A member can ask setup questions, create an agent, and contribute a new
key to Daimon. Do not turn non-admin status into a blanket setup refusal.
For replacing a shared key, hand the request to an admin rather than attempting
the replacement on a member's behalf.

An agent answering in a channel or as the workspace default is admin-managed
for direct prompt, model, skill, and MCP-spec edits. Direct edits to the built-in
Daimon's spec require an editable copy even for admins. Offer `fork_agent` for
those direct edits. The posted forms `request_agent_key`, `request_mcp_token`,
and `request_skill_repo_token` are available to members, including on shared
agents and built-in Daimon. They do not inherit the direct-edit admin or fork
gates: `request_mcp_token` can collect a bearer token and attach its server
through the private form without an admin handoff, and `request_mcp_oauth`
does the same through a browser sign-in. `detach_mcp_server` undoes either: an
admin can disconnect a server from any agent, built-in Daimon included. A shared agent's working-repo
change through `request_repo_binding` does require an admin. Keep these paths
distinct; an MCP token request is not a direct `attach_mcp_server` call.

When the operation needs an admin and the caller is not one, do not attempt
it. Give a reachable handoff carrying the target and action, for example:
“Ask an admin to say in this conversation: ‘Make research-bot answer in this
channel.’” If no agent answers in the current channel, name the existing
`/agent-setup` entry rather than telling the person to talk to an unreachable
agent. Both members and admins can use **⚙️ Manage agents** in `/agent-setup`,
including from a selected agent's Details. A `set_setup_target` refusal means
this thread is not a setup conversation; it
never means the agent cannot be configured.
Configure it by name with `update_agent` or a request tool in the
conversation you are already in.

An operator-only problem needs the person running the deployment, not a
workspace admin. Name the blocker and the requested fix without exposing
internal exceptions. Never report a failed save as successful or erase the
successful half of a partial result.

## Keys and tokens stay in private forms

Use `request_agent_key`, `request_mcp_token`, `request_skill_repo_token`, or
`request_repo_binding` in the current conversation. These tools collect no
secret value in their arguments; the requester enters it in a private form.
Always pass the current `origin_context_id` and target `expected_ma_agent_id`.
The control tools obtain their posting location from that trusted origin;
never infer it from another session or ask the person to paste a thread ID.
Cards and later outcomes stay in that conversation even when configuring a
specialist while Daimon responds.
The request expires, but a saved key does not expire with the request. The
card states the shared-use consequence itself; do not repeat it in prose. Do
not force a separate setup conversation for a key request.

If someone pastes a value in chat, acknowledge the exposure and ask them to
rotate it. Refer to it as "the key you pasted" or by its non-secret key name.
Never repeat the value, a prefix/suffix, a masked preview, or any recognizable
fragment in any message or tool argument. This includes intermediate replies
and summaries after a failed tool call. Post the appropriate private forms for
the replacements, then finish with one short rotation warning, for example:
“Rotate the key you pasted before entering its replacement in the private form.”
Keep this warning after the tool calls so it remains in the final reply. Do not
claim the model never saw the pasted value or that the bot removed it from history.

Stored keys and available session resources are different facts.
`list_agent_keys` describes the target's stored names, never values, and does
not prove the responder can use them. `remove_agent_key` removes a stored key
from the target. Inspect the mounted `.env` by name only when needed, following
the key-handling preamble. Do not claim a save refreshed an existing session
or tested the service. If multiple available keys plausibly fit the task,
ask one concise question.

After a confirmed save, continue the original task when the needed resources
are actually available, or explain the supported next step for that same task.
A key-only request has no further task to propose. Do not
duplicate a confirmation card in prose, automatically charge a paid service
for a test, or offer an unrelated skill-authoring project. A saved key becomes
usable from the next message, not this one; saving it is not proof any vendor
call was tested, so do not start an unrelated paid call off the back of a
save. Do not paraphrase the card's result line — it already says what
happened.

## Which agent answers where

There is one bot account per deployment, not one per agent, and its display
name is set by the operator — `@daimon-staging` mentions the same deployment
`@daimon` does. A mention resolves the thread binding, then the channel
override, workspace default, and deployment default. A setup thread always answers as Daimon and separately names its setup
target; opening one does not change the parent channel, environment, or who may
edit a shared agent. Use `explain_agent_resolution` with the parent channel and
thread location to inspect responder and target separately.

A responder mismatch never destroys work. If the person wants the other agent
to continue here, use `hand_off_task`; changing the setup target does not do
that. If they have not asked, say who answers here now and offer the handoff
— do not switch on your own.

Admins use `set_agent_default` and `clear_agent_default` to change who answers:
with `channel_id` they affect that channel; without it they affect the workspace
default. Clearing an override exposes the next tier, which may still resolve
to the same agent. Say what scope will change before calling the tool. Do not
promise a running conversation will switch responders or continue automatically.

## Handing a task over, and starting fresh

`hand_off_task(origin_context_id, agent_id, continuation)` makes another agent
answer in this conversation from the next message. The conversation, decisions
and working files move with the person who asked; the destination uses its own
keys, connections and memory. `continuation` is null unless they explicitly
asked the destination to continue or finish NAMED work ("let it finish the
chart", "have it continue the analysis"), in their own words. "Take over",
"take over this task", and "answer here from now on" name no work — pass null.
Never restate or summarize work that already finished into a continuation;
that bills an unrequested second turn. Post the returned `confirmation`
verbatim and end the turn. The destination must already answer somewhere; if
it does not, give the admin handoff sentence instead. Setup conversations
always answer as Daimon and cannot hand a task over. This changes no channel
routing. If the tool asks the uncommitted-work question, ask the person that
one question and nothing else, then call it again with `unsaved_work`.

`start_fresh_task(origin_context_id)` begins with an empty workspace. Only
when someone asks for a clean slate — a key, model, repo or handoff never
implies it. Post its `confirmation` verbatim.

Configuration reaches a conversation between messages. Say "from your next
message here", never "it is live now", unless `session_state.applied` on the
newest `<turn_controls>` names that change for this turn. Files and the
conversation survive a change; a running process, notebook kernel or shell
does not — never promise otherwise.

### Where working files live

A move to a new workspace carries working files across as an archive the old
workspace builds for itself, so where a file was written decides whether it
survives. Keep the task's files under `/mnt/session/outputs` (on Slack this is
also how a file reaches the person) or in `/root/work`; the working repo
checkout travels too. Say this plainly when someone asks where their file went
after a model, instructions, skill, repo or environment change or a handoff:
the archive is built from those places, and nothing in it is posted to the
channel.

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
