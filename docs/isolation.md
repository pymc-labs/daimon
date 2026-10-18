# Isolation

"Isolated" in Daimon means one party cannot read or change another's copy of
a thing. This page says, for each thing Daimon keeps, which scope owns it and
what is shared across that scope. It is written from the code; every claim
names the file and line that enforces it.

The rules that *move* content between channels (channel rules, agent rules,
`readers: own`) are in [permissions.md](permissions.md). This page is about
ownership: who has their own copy, and who shares one.

## Scopes

| Scope | What it is | Identified by |
| --- | --- | --- |
| Tenant | one Discord guild, Slack workspace or Microsoft 365 organisation | `derive_tenant_uuid` (`packages/core/daimon/core/ma_identity.py:49`) |
| Channel | a channel in a tenant | the platform channel id |
| Thread | a Discord thread, Slack thread or Teams reply chain | the platform thread id |
| Caller | the person whose message started the turn | their `accounts` row (`account_id`) and platform user id |
| Agent | one Managed Agents (MA) agent in a tenant | `derive_agent_uuid` (`packages/core/daimon/core/ma_identity.py:34`) |
| DM | a private conversation opened with `/dm`, or a Slack private turn | the DM scope id, stamped on the session as `daimon_private_dm` |

One deployment runs on one Anthropic API key, so every tenant's agents,
sessions, files, memory stores and skills live in **one MA workspace**.
Tenant separation inside MA is a metadata stamp or a name prefix, not an MA
permission boundary (see [Known gaps](#known-gaps)).

## At a glance

"Own" means the scope has its own copy. "Shared" means everyone in the
scope uses the same one.

| Resource | Tenant | Channel | Thread | Caller | Agent | DM |
| --- | --- | --- | --- | --- | --- | --- |
| MA session and its transcript | own | own | own | **own** | own | own, private |
| Sandbox working files | own | own | own | **own** (one sandbox per session) | own | own |
| Session outputs (Files API) | own | own | own | **own** (listed by session) | own | own |
| Chat attachments | n/a | visible to channel readers | **shared, by design**: visible to everyone in the thread, and replayed history can carry them into other participants' agents | posted by the caller | n/a | not delivered |
| Published notebooks | own | shared by link | shared by link | managed by the publisher; opened by anyone with the link | n/a | shared by link |
| Agent memory, shared store (`/mnt/memory/daimon-…`) | own | **shared** | **shared** | **shared** | **own** (one store per agent) | shared, optionally read-only |
| Agent memory, personal store (`/mnt/memory/personal-…`) | own | follows the caller | follows the caller | **own** (one per person per agent) | **own** | the caller's own, optionally read-only |
| Agent keys (`.env`) | own | shared | shared | **shared** | **own** | shared |
| Token MCP credentials | own | shared | shared | **shared** | **own** | shared |
| OAuth MCP credentials | own | shared | shared | **own** | own | own |
| Daimon MCP vault | own | shared | shared | **own** | own | Slack: own per turn |
| Agent repo binding | own | shared | shared | **shared** | **own** | shared |
| Agents | **own** | shared, unless a rule keeps one in a channel | shared | shared | n/a | shared |
| Skills | own (by name prefix) | channel skills are per channel | shared | shared | per agent, plus the tenant library | none from the channel |
| Prepaid balance (ledger) | **own** | shared | shared | shared | shared | shared |
| Channel budget | own | **own** | counts toward its channel | shared | shared | counts toward the source channel |
| Per-member cap | own | shared | shared | **own** | shared | own |
| Routines | own | a `readers: own` channel's routines are visible only inside it | n/a | **own** (creator or admin) | run the agent's shared keys and memory | n/a |

The rest of the page explains each row.

## MA sessions

A thread does not have one session. It has one live session **per caller**.
`thread_sessions` rows carry `account_id`
(`packages/core/daimon/core/_models.py:476`), and the turn pipeline's lookup
matches it by plain equality
(`packages/core/daimon/core/stores/thread_sessions.py:41-72`): a row with a
NULL `account_id` matches nobody. Every adapter passes the caller's own
account: Discord (`packages/adapters/discord/daimon/adapters/discord/bot.py:3343`,
`packages/adapters/discord/daimon/adapters/discord/bot.py:4146`,
`packages/adapters/discord/daimon/adapters/discord/wizard_submit.py:537`),
Slack (`packages/adapters/slack/daimon/adapters/slack/app.py:2353`) and Teams
(`packages/adapters/teams/daimon/adapters/teams/app.py:1117`). The setting that
put every caller in a thread on one shared session was removed in `f4f1efc7d`.

Each session is stamped with its tenant and owning account
(`packages/core/daimon/core/sessions.py:96-120`). The transcript tools open
only sessions stamped with the caller's own account
(`packages/adapters/mcp/daimon/adapters/mcp/tools/_session_access.py:190-216`).
The one exception is a hub read by a server admin of a channel conversation, never a private DM and never to continue it.

When a session is replaced (configuration change, recovery), the carried
work comes only from the caller's own previous row: the replacement looks up
the live row with the caller's account
(`packages/core/daimon/core/session_preparation.py:677-683`), checkpoints that
row's own session (`packages/core/daimon/core/session_preparation.py:394-404`)
and bundles that session's files
(`packages/core/daimon/core/workspace_transfer.py:301`).

## Sandbox working files

Each MA session runs in its own sandbox, created by `sessions.create`
(`packages/core/daimon/core/sessions.py:613`). Two callers in one thread
therefore have two sandboxes and never see each other's working files
directly. The **environment** (packages, networking) is a template chosen per
channel, tenant or deployment
(`packages/core/daimon/core/channel_environments.py:1-14`); it is shared
configuration, not shared storage.

## Session outputs (Files API)

Files an agent writes for delivery are listed by session:
`files.list(scope_id=session_id)`
(`packages/core/daimon/core/output_delivery.py:167-168`). A sweep only ever
reads the session of the turn it follows, and sweeps are serialised per
session (`packages/adapters/discord/daimon/adapters/discord/bot.py:786-812`).
A delivered file is then **posted into the thread**
(`packages/adapters/discord/daimon/adapters/discord/output_delivery.py:99-104`),
where everyone in the thread can see it, and later turns, including other
callers' turns, can receive it through replayed history, subject to the
limits in the next section.

Files staged with `create_file_upload_url` and posted by handle are keyed by
tenant only (`packages/core/daimon/core/stores/file_uploads.py:99-118`). The
handle is a random bearer value
(`packages/core/daimon/core/stores/file_uploads.py:57`), so only a session
that was told the handle can post it.

Published notebooks have two layers. *Managing* one (publishing under a
slug, listing, deleting) is namespaced by the caller's account
(`packages/adapters/mcp/daimon/adapters/mcp/tools/notebook.py:203`). *Opening*
one is not: the URL carries an access token that is the notebook's only key
(`packages/adapters/mcp/daimon/adapters/mcp/tools/notebook.py:184`,
`apps/notebook-host/src/notebook_host/lifecycle.py:102-110`), and anyone
holding an editable link can run code on the notebook host
(`packages/adapters/mcp/daimon/adapters/mcp/tools/notebook.py:172`). A link
posted in a thread is open to everyone who reads it.

## Chat attachments and thread history

A person's attachments reach a session in two ways:

- **Inlined images** are downloaded and sent with the turn as image blocks:
  - Discord: the triggering message's images, as base64 blocks with a URL
    line as well
    (`packages/adapters/discord/daimon/adapters/discord/bot.py:4433-4436`).
  - Slack: the triggering message's images
    (`packages/adapters/slack/daimon/adapters/slack/app.py:2608-2613`, passed
    to the turn at `packages/adapters/slack/daimon/adapters/slack/app.py:2805`).
  - Teams: the triggering message's images **and** up to four images from
    earlier messages in the replayed history, from any author
    (`packages/adapters/teams/daimon/adapters/teams/app.py:1164-1172`,
    `packages/adapters/teams/daimon/adapters/teams/app.py:1231`,
    `packages/adapters/teams/daimon/adapters/teams/context.py:40`,
    `packages/adapters/teams/daimon/adapters/teams/context.py:315-325`).
    On Teams another caller's image can therefore reach a turn as bytes, not
    only as a URL.
- **Attachments that are not inlined** are written into the turn's text as a
  URL the agent can fetch: the triggering message's data files as `[attachment]` lines
  (`packages/adapters/discord/daimon/adapters/discord/attachments.py:16-40`),
  and files on earlier messages in the thread, **from any author**, as
  `<attachment>` elements inside the replayed history.

Replayed history is bounded, so it can carry other callers' files but does
not always carry all of them:

- **Discord.** A caller's first turn gets up to 100 messages
  (`packages/adapters/discord/daimon/adapters/discord/bot.py:4398`). A later
  turn gets the messages since that caller's own last turn
  (`packages/adapters/discord/daimon/adapters/discord/bot.py:4375`), up to
  discord.py's default of 100
  (`packages/adapters/discord/daimon/adapters/discord/context.py:551`). Each
  file carries its signed CDN URL, which expires after about a day
  (`packages/adapters/discord/daimon/adapters/discord/context.py:297-301`).
  Only the caller's own earlier answers are left out
  (`packages/adapters/discord/daimon/adapters/discord/bot.py:4386`).
- **Slack.** The first turn replays one page from the thread root and marks
  the block `truncated` when the thread is longer
  (`packages/adapters/slack/daimon/adapters/slack/context.py:151-184`); a
  later turn likewise fetches one page of messages since its watermark
  (`packages/adapters/slack/daimon/adapters/slack/context.py:221-242`). Files
  carry a signed proxy URL that fetches with the bot token, and are left out
  when no proxy is configured
  (`packages/adapters/slack/daimon/adapters/slack/context.py:100-113`).
- **Teams.** At most 10 history files are resolved
  (`packages/adapters/teams/daimon/adapters/teams/context.py:41`,
  `packages/adapters/teams/daimon/adapters/teams/context.py:325`), only when
  the bot has file access
  (`packages/adapters/teams/daimon/adapters/teams/thread_reader.py:134-144`).
  A file without a download URL is listed as `fetchable="false"`
  (`packages/adapters/teams/daimon/adapters/teams/context.py:190-195`).

Within those bounds, attachments are **per thread, by design**: the thread is
a shared room, and the agent sees what a reader of the thread sees.

**Thread files are visible to everyone in the thread, including other
participants' agents (by design, decided 2026-10-12).** This covers files
people attach and files Daimon delivers into the thread. A file that should
stay with one person belongs in a DM, not a shared thread.

## Agent memory

An agent has two kinds of memory store (`packages/core/daimon/core/memory_resource.py`):

- **Shared**: one MA memory store per (tenant, agent), mounted on every
  ordinary session of that agent, for every person, channel, thread and DM.
  Its instructions keep it to team and agent knowledge (conventions, project
  facts, how tools and skills behave) and forbid recording anything personal.
- **Personal**: one store per (tenant, agent, account), mounted only on that
  person's own sessions. A chat session is already one per caller, so another
  person's session never has it mounted and cannot read it; that separation is
  the mount, not the prompt. Only which store a fact is written to depends on
  the agent following its instructions.

Personal stores are mounted on chat turns (channels, threads and DMs on every
platform), on the hub's `ask` and `start_turn` (the caller is verified by their
platform sign-in and only their account can use the session), and on a routine
whose result reaches only its owner (no destination and no channel), read-only.
Per-agent keys, operator and CLI tokens, `daimon sessions create` and routines
with a destination mount the shared store alone. A person sees and clears their
own personal store with `/memory mine` and `/memory forget-mine`; nobody can
see or clear anyone else's. Details are in
[architecture.md](architecture.md#shared-and-personal-memory).

Both mounts of a session have the same access. They are read-only:

- in a channel or thread with limited readers, unless the agent is that
  channel's own (`packages/core/daimon/core/turn/admission.py:560-564`);
- in a DM when the tenant sets `dm_memory_read_only`
  (`packages/core/daimon/core/turn/admission.py:563`);
- in a routine run (`packages/core/daimon/core/headless_runner.py:335`).

Read-only stops a turn from writing; it does not hide what others wrote to the
shared store.

The exception is an isolated session, which an agent-chat turn started from
an uploaded bundle uses
(`packages/adapters/mcp/daimon/adapters/mcp/tools/agent_chat.py:516`):
`create_isolated_session` mounts no vault, `.env`, memory store or repo, only
the resources its caller passes
(`packages/core/daimon/core/sessions.py:681-718`). "Every session" below means
every ordinary session created by `create_session`.

## Keys and credentials

| Credential | Keyed by | Shared with |
| --- | --- | --- |
| Agent keys, mounted as `.env` | (tenant, agent) (`packages/core/daimon/core/_models.py:1239-1246`) | every session of the agent, except one for a person from another organisation (`packages/core/daimon/core/sessions.py:851-862`) |
| Token MCP server credentials | (tenant, agent) (`packages/core/daimon/core/agent_mcp_credentials.py:170`) | mirrored into every caller's vault except a person from another organisation's, whose session leaves the server off unless they signed in to it (`packages/core/daimon/core/sessions.py:812-848`) |
| OAuth MCP server grants | (account, agent) vault | the person who signed in only. The server is hidden from everyone else's session unless the agent also holds a shared token for that URL, in which case others use the token (`packages/core/daimon/core/sessions.py:506`, `packages/core/daimon/core/mcp_personal_servers.py:63-90`) |
| Daimon MCP vault (the sandbox's call-back JWT) | (account, agent) (`packages/core/daimon/core/mcp_vault.py:236`) | the caller only; a Slack private turn gets its own execution vault |
| Per-agent GitHub PAT | the agent (`packages/core/daimon/core/sessions.py:338`) | every session of the agent |
| GitHub App access | the requester (`packages/core/daimon/core/sessions.py:594-605`) | the caller only |

A member may add a new key name to an agent
(`packages/core/daimon/core/env_file.py`). Replacing or removing a key on an
agent other people use needs an admin; "other people" includes another
account with a live session on the agent
(`packages/core/daimon/core/stores/scoped_config_read.py:255-283`).

## Agents and forks

Agents are tenant objects: the resolver rejects one whose `daimon_tenant`
stamp is another tenant's (`packages/core/daimon/core/ma_resolver.py:169`,
`packages/core/daimon/core/ma_resolver.py:186`). Inside a tenant an agent is
shared by every channel that routes to it, unless an agent rule keeps it in
one channel ([permissions.md](permissions.md#agent-rules)). A thread handed to
another agent changes the agent for everyone in that thread: the binding is
one row per thread (`packages/core/daimon/core/_models.py:2229-2238`,
`packages/core/daimon/core/thread_handoff.py:277`).

A fork copies the prompt, model, tools and MCP definitions, but no
credentials (`packages/core/daimon/core/agent_fork.py:243`). Its skills split
three ways (`packages/core/daimon/core/agent_fork.py:115-133`):

- skills owned by the source agent are uploaded again under the copy's name,
  so the copy gets new ids for them;
- tenant-library and built-in skills, which no agent owns, are kept by id
  (`packages/core/daimon/core/agent_fork.py:126`,
  `packages/core/daimon/core/agent_fork.py:259`), so source and copy share
  them;
- another agent's skills, and any that fail to copy, are left off and named.

## Skills

MA skills are workspace-wide objects, and the workspace is shared by every
tenant on the deployment. Daimon claims a skill for a tenant by its title:
`{first 8 hex of tenant id}-{name}` for the tenant library and
`{t8}-{agent}/{name}` for one agent
(`packages/core/daimon/core/defaults/metadata.py:160-184`), and filters lists
by that prefix (`packages/core/daimon/core/defaults/metadata.py:273-283`).
Within a tenant, an agent's skills reach every caller of that agent. Channel
skills are added per channel and apply to that channel's turns only
(`packages/core/daimon/core/sessions.py:556`); a DM turn gets none
(`packages/core/daimon/core/turn/admission.py:539`).

## Billing

- **Balance.** One prepaid ledger per tenant (`tenant_ledger`,
  `packages/core/daimon/core/_models.py:961`). Every turn in the tenant draws
  on it.
- **Channel budget.** Spend is counted per channel
  (`packages/core/daimon/core/channel_budget.py:234`). A thread counts toward
  its parent; a DM toward the channel it was moved from; an agent kept in one
  channel toward its home (`packages/core/daimon/core/turn/admission.py:502`).
  Everyone in a channel shares its budget.
- **Per-member cap.** One row per (tenant, platform user), with a tenant-wide
  default (`packages/core/daimon/core/_models.py:878-893`), checked against
  that person's own monthly spend (`packages/core/daimon/core/billing.py:86`).

See [billing.md](billing.md).

## Routines

A routine belongs to its creator: only the creator or an admin lists, reads,
pauses or deletes it (MCP read gate
`packages/adapters/mcp/daimon/adapters/mcp/tools/routines.py:115-124`, mutation
gate `packages/adapters/mcp/daimon/adapters/mcp/tools/routines.py:93-112`; panels
`packages/core/daimon/core/routines.py:43-49`). A routine of a `readers: own`
channel, or of that channel's own agent, is in addition visible only from
inside that channel
(`packages/adapters/mcp/daimon/adapters/mcp/tools/routines.py:340-343`). Each fire
opens a fresh session on the creator's account with read-only memory, and the
session is stamped private so only its owner reads the transcript. A routine
still runs with its agent's shared keys, connectors, repo and shared memory; only
one whose result reaches nobody but its owner also reads the owner's personal
memory. See
[routines.md](routines.md#who-may-do-what).

## DMs

A DM is its own scope with its own per-account session, stamped
`daimon_private_dm` (`packages/core/daimon/core/turn/admission.py:593`). The
MCP session tools open it only with the matching execution grant, admins
included (`packages/adapters/mcp/daimon/adapters/mcp/tools/_session_access.py:198-215`).
A Slack private turn carries that grant, so its own owner can inspect it. A
Discord private scope carries none, so nobody can inspect it through MCP, not
even its owner. It shares the agent's shared memory and mounts the person's own
personal memory (both read-only if the tenant says so), keys and connectors, and its spend counts toward the channel it came
from. Details are in
[architecture.md](architecture.md#opt-in-dm-conversations).

## Two people in one thread

Per-caller sessions are always on, so two people in one thread never share an
MA session, sandbox, transcript or output listing. These are the paths by
which something of one person's can still reach the other's turn.

**Thread files are visible to everyone in the thread, including other
participants' agents (by design, decided 2026-10-12).** Paths 1 and 2 below
are therefore intended behaviour, not a leak; the rest are listed so the
boundary is explicit.

1. **Thread history.** Each turn's context can carry the other person's
   messages, Daimon's answers to them, and fetchable URLs for files in the
   thread, theirs and the ones Daimon delivered to them, within the history
   window, configuration and URL expiry described
   [above](#chat-attachments-and-thread-history)
   (`packages/adapters/discord/daimon/adapters/discord/context.py:297-301`,
   `packages/adapters/discord/daimon/adapters/discord/bot.py:4375-4410`;
   Slack `packages/adapters/slack/daimon/adapters/slack/context.py:100-113`;
   Teams `packages/adapters/teams/daimon/adapters/teams/context.py:190-195`).
   The second person's agent can fetch those files into its own sandbox and
   send them again. On Teams, up to four of the other person's history
   images are also inlined into the turn as image blocks
   (`packages/adapters/teams/daimon/adapters/teams/context.py:315-325`).
2. **Delivered outputs.** A file the first person's session produced is
   posted into the thread
   (`packages/adapters/discord/daimon/adapters/discord/output_delivery.py:99-104`),
   so it is visible to both, and reaches the second person's session by
   path 1.
3. **Published notebook links.** A notebook link Daimon posts for one person
   carries its access token
   (`apps/notebook-host/src/notebook_host/lifecycle.py:102-110`), so the
   other person can open it, and run code in it if it is editable
   (`packages/adapters/mcp/daimon/adapters/mcp/tools/notebook.py:172`).
   Only listing and deleting stay with the publisher's account.
4. **Shared agent memory.** Both sessions mount the agent's shared store;
   what one writes there the other reads, in this thread and every other.
   Each person's personal store is mounted only in their own session and does
   not cross.
5. **Agent keys, token connectors and the repo.** Both sessions mount the
   same `.env` (`packages/core/daimon/core/sessions.py:417-425`), the same
   token MCP credentials (`packages/core/daimon/core/sessions.py:390-411`) and
   clone the same bound repo (`packages/core/daimon/core/sessions.py:430-465`).
   A key one person adds is in the other's next session; a branch one session
   pushes is visible to the other's clone.
6. **The thread's agent.** A handoff, or a Switch to agent click, rebinds the
   thread for both (`packages/core/daimon/core/thread_handoff.py:277`).
7. **Feedback and support attribution.** A down-vote or support request
   attaches the newest live session in the thread, whoever owns it
   (`packages/core/daimon/core/stores/thread_sessions.py:233-264`, used in
   `packages/adapters/discord/daimon/adapters/discord/support_escalation.py:237`
   and `packages/adapters/discord/daimon/adapters/discord/feedback_modal.py:114`).
   The operator who reads the report may be pointed at the other person's
   session.
8. **The turn queue.** Mentions in a thread are serialised per thread, not
   per session, so one person's turn waits for the other's to finish. The
   queued messages are still grouped by author
   (`packages/adapters/discord/daimon/adapters/discord/bot.py:2732`), so no
   one's message runs in another person's turn.

What does **not** cross: the session and its transcript, the sandbox, the
Files API output listing, the carried work on a session replacement, OAuth
connector grants, personal memory, the Daimon MCP vault, notebook management (listing and
deleting), the per-member cap and routines.

## Known gaps

These are places where the code and its own description disagree today.

- The architecture page describes `thread_sessions` as mapping
  `(tenant, platform, thread)` to one MA session
  ([architecture.md](architecture.md#sessions-and-managed-agents)); it is one
  live session per caller
  (`packages/core/daimon/core/_models.py:452-461`).
- Code comments still describe one session per thread
  (`packages/adapters/discord/daimon/adapters/discord/bot.py:2528-2531`,
  `packages/adapters/discord/daimon/adapters/discord/wizard_submit.py:50-59`).
- `get_latest_thread_session` says it serves only message-feedback
  attribution (`packages/core/daimon/core/stores/thread_sessions.py:17-20`),
  but support escalations on all three platforms also use it and store the
  session id it returns, which may be another caller's.
- Shared memory written before personal stores existed may still hold one
  person's preferences; nothing was moved. `daimon memory personal-report`
  lists, per agent, how many shared entries look personal, redacted.
- A tenant owns MA skills by the first 8 hex characters of its id
  (`packages/core/daimon/core/defaults/metadata.py:177`,
  `packages/core/daimon/core/defaults/metadata.py:280`), a 32-bit prefix,
  while agents and environments carry the full tenant id. Two tenants on one
  deployment with the same prefix would see each other's skills.
- The fork module's description says the copy and its source "never share a
  skill id" (`packages/core/daimon/core/agent_fork.py:6-8`). Only the source's
  own skills get new ids; library and built-in skills stay shared
  (`packages/core/daimon/core/agent_fork.py:126`).
