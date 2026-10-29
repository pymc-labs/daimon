# Isolation

Which scope owns each thing Daimon keeps, and what is shared across it. Read it
to know who can see what before you invite people into a shared channel or
thread.

"Isolated" means one party cannot read or change another's copy of a thing.
The rules that move content between channels (channel rules, agent rules,
`readers: own`) are in [permissions.md](permissions.md). This page is about
ownership.

## Scopes

| Scope | What it is |
| --- | --- |
| Tenant | one Discord server, Slack workspace or Microsoft 365 organisation |
| Channel | a channel in a tenant |
| Thread | a Discord thread, Slack thread or Teams reply chain |
| Caller | the person whose message started the turn |
| Agent | one Managed Agents (MA) agent in a tenant |
| DM | a private conversation opened with `/dm-me`, or a Slack private turn |

One deployment runs on one provider key, so every tenant's agents, sessions,
files, memory stores and skills live in one MA workspace. Separation inside MA
is a metadata stamp or a name prefix, not an MA permission boundary.

## At a glance

"Own" means the scope has its own copy. "Shared" means everyone in the scope
uses the same one.

| Resource | Tenant | Channel | Thread | Caller | Agent | DM |
| --- | --- | --- | --- | --- | --- | --- |
| MA session and transcript | own | own | own | **own** | own | own, private |
| Sandbox working files | own | own | own | **own** | own | own |
| Session outputs | own | own | own | **own** | own | own |
| Chat attachments | n/a | visible to channel readers | **shared, by design** | posted by the caller | n/a | not delivered |
| Published notebooks | own | shared by link | shared by link | managed by the publisher | n/a | shared by link |
| Shared memory store | own | **shared** | **shared** | **shared** | **own** | shared, optionally read-only |
| Personal memory store | own | follows the caller | follows the caller | **own** | **own** | the caller's own |
| Agent keys (`.env`) | own | shared | shared | **shared** | **own** | shared |
| Token MCP credentials | own | shared | shared | **shared** | **own** | shared |
| OAuth MCP credentials | own | shared | shared | **own** | own | own |
| Daimon MCP vault | own | shared | shared | **own** | own | Slack: own per turn |
| Agent repo binding | own | shared | shared | **shared** | **own** | shared |
| Agents | **own** | shared, unless a rule keeps one in a channel | shared | shared | n/a | shared |
| Skills | own, by name prefix | channel skills per channel | shared | shared | per agent, plus the tenant library | none from the channel |
| Prepaid balance | **own** | shared | shared | shared | shared | shared |
| Channel budget | own | **own** | counts toward its channel | shared | shared | counts toward the source channel |
| Per-member cap | own | shared | shared | **own** | shared | own |
| Routines | own | visible only inside a `readers: own` channel | n/a | **own** (creator or admin) | run the agent's shared keys and memory | n/a |

## Sessions, files and attachments

A thread has one live session per caller, not one session in all. Two people
in one thread never share an MA session, sandbox, transcript or output
listing. Each session is stamped with its tenant and owner, and the transcript
tools open only the caller's own sessions. The exception is a server admin
reading a channel conversation through the hub, never a private DM and never
to continue it.

When a session is replaced, for example after a configuration change, the
carried work comes only from the caller's own previous session.

Each session runs in its own sandbox. The environment (packages, networking)
is a template chosen per channel, tenant or deployment. It is shared
configuration, not shared storage.

Files an agent writes for delivery are listed by session, then posted into the
thread, where everyone in it can see them. Later turns, including other
callers' turns, can receive them through replayed history.

Published notebooks have two layers. Listing and deleting one is limited to the
publisher's account. Opening one is not: the link carries an access token that
is the notebook's only key, and anyone holding an editable link can run code
on the notebook host. A link posted in a thread is open to everyone who reads
it.

### Thread attachments are shared

Attachments are per thread, by design. The thread is a shared room, and the
agent sees what a reader of the thread sees. That covers files people attach
and files Daimon delivers. A file that should stay with one person belongs in a
DM, not a shared thread.

Images on the triggering message are sent to the agent with the turn. Other
files become URLs in the turn's text. Replayed history carries files from
earlier messages by any author, within bounds:

| Platform | History replayed |
| --- | --- |
| Discord | up to 100 messages on a caller's first turn, then messages since that caller's last turn; file URLs expire after about a day |
| Slack | one page from the thread root, then one page since the last turn; files are left out when no proxy is configured |
| Teams | at most 10 history files, only when the bot has file access; up to four earlier images from any author are inlined |

## Agent memory

An agent has two kinds of memory store:

- **Shared**: one per agent, mounted on every ordinary session of that agent,
  for every person, channel, thread and DM. Its instructions keep it to team
  and agent knowledge such as conventions and project facts.
- **Personal**: one per agent and person, mounted only on that person's own
  sessions. Another person's session never has it mounted, so the separation
  is the mount, not the prompt. Only the choice of which store a fact goes to
  depends on the agent following its instructions.

Personal stores are mounted on chat turns on every platform, on the hub's `ask`
and `start_turn`, and read-only on a routine whose result reaches only its
owner. Per-agent keys, operator and CLI tokens, `daimon sessions create` and
routines with a destination get the shared store alone.

Both mounts are read-only in three cases. This stops a turn from writing; it
does not hide what others wrote to the shared store.

- A channel or thread with limited readers, unless the agent is that channel's
  own.
- A DM, when the tenant sets `dm_memory_read_only`.
- A routine run.

Tightening access replaces an idle writable session. An active session refuses
the restricted turn instead. The replacement skips the old session's checkpoint
and uncommitted workspace files, and history supplies the new turn's context.
An isolated agent-chat session started from an uploaded bundle mounts no
vault, keys, memory or repo.

People manage their own store in chat:

| Command | Effect |
| --- | --- |
| `/memory` (Discord, Slack), `memory` (Teams) | show the shared store, read-only |
| `/memory mine` | list your personal store; `mine /<path>` shows one file |
| `/memory forget-mine` | preview deletion and a six-character code |
| `/memory forget-mine <code>` | delete every memory in your store |

In a place held for outside members or a shared channel, `/memory` shows the
channel's own store, and `mine` and `forget-mine` touch nothing. A store that
changed since the preview needs a new code, and memories written in between are
kept. Nobody can see or clear anyone else's store. Deleted memories keep their
MA version history for 30 days. Deleting an agent archives its stores, and
account deletion deletes every personal store, archived ones included.

Memory written before personal stores existed may still hold personal facts.
`daimon memory personal-report` lists, per agent, how many shared entries look
personal, redacted. Nothing is moved automatically.

## Keys and credentials

| Credential | Keyed by | Shared with |
| --- | --- | --- |
| Agent keys, mounted as `.env` | tenant, agent | every session of the agent |
| Token MCP server credentials | tenant, agent | mirrored into every caller's vault |
| OAuth MCP server grants | account, agent | the person who signed in only; hidden from others unless the agent also holds a shared token for that URL |
| Daimon MCP vault (call-back JWT) | account, agent | the caller only |
| Per-agent GitHub PAT | the agent | every session of the agent |
| GitHub App access | the requester | the caller only |

A member may add a new key name to an agent. Replacing or removing a key on an
agent other people use needs an admin.

## Agents, forks and skills

Agents are tenant objects, and one tenant's agent cannot be resolved by
another. Inside a tenant an agent is shared by every channel that routes to it,
unless an agent rule keeps it in one channel. A thread handed to another agent
changes the agent for everyone in that thread.

A fork copies the prompt, model, tools and MCP definitions, but no credentials.
Skills split three ways:

- Skills the source agent owns are uploaded again under the copy's name.
- Tenant-library and built-in skills are kept by id, so source and copy share
  them.
- Another agent's skills, and any that fail to copy, are left off and named.

MA skills are workspace-wide, so Daimon claims a skill for a tenant by a title
prefix: the first 8 hex characters of the tenant id. Lists are filtered by it.
That is a 32-bit prefix, so two tenants on one deployment with the same prefix
would see each other's skills. Agents and environments carry the full id.

## Billing and routines

One prepaid ledger serves the whole tenant. A channel budget counts a thread
toward its channel, a DM toward the channel it came from, and an agent kept in
one channel toward its home. The per-member cap is one row per person with a
tenant default. See [billing.md](billing.md).

A routine belongs to its creator: only the creator or an admin lists, reads,
pauses or deletes it. A routine of a `readers: own` channel is visible only
from inside that channel. Each fire opens a fresh session on the creator's
account, stamped private so only the owner reads the transcript. It still runs
with the agent's shared keys, connectors, repo and shared memory. See
[routines.md](routines.md).

## Direct messages

A DM is its own scope with its own per-account session, stamped private. The
MCP session tools open it only with a matching execution grant, admins
included. A Slack private turn carries that grant, so its owner can inspect it.
A Discord private scope carries none, so nobody can, not even its owner.

A DM shares the agent's shared memory and mounts the person's personal memory,
both read-only if the tenant says so. Its spend counts toward the channel it
came from.

### Agent-initiated DMs

The channel tool `send_direct_message(recipient_id, content)` sends a DM on
Discord, Slack or Teams. Sender and recipient must both be current members, and
bots, inactive, external and anonymous users are rejected. It sends at most
19,000 characters as plain text, with no attachments and no cross-tenant
delivery. The default policy is tenant members. Set a tenant to `allowlist`
(with `recipient_ids`) or `disabled` in `DAIMON_DIRECT_MESSAGE_POLICIES`
([configuration.md](configuration.md#daimon_direct_message_policies)).

### Opt-in DM conversations

DM conversations are off until the tenant enables them. An admin runs
`/dm-me enable` or `/dm-me disable`. Disabling stops new turns without
cancelling a running one. `/dm-me` in a channel moves its recent text and a
source link into a new private scope. Run it again to reset or pick another
channel. Replies are text only, delivered when the turn completes.

`/dm-me` is refused from a channel with limited readers, or a Discord thread
under one, because the DM sits outside the rule and could reach open channels.
A DM whose source later limits its readers is quarantined: the next turn is
refused, its copied context and private history are deleted, and its sessions
are retired. A fresh `/dm-me` is needed.

## Two people in one thread

Per-caller sessions mean nothing in a session crosses between two people. These
paths still carry something from one person to the other.

1. **Thread history.** Each turn's context can carry the other person's
   messages, Daimon's answers and fetchable file URLs. The second person's agent
   can fetch those files into its sandbox and send them again.
2. **Delivered outputs.** A file one session produced is posted to the thread,
   so both see it.
3. **Notebook links.** A link carries its access token, so the other person can
   open it, and run code in it if it is editable.
4. **Shared agent memory.** What one session writes there, the other reads, in
   this thread and every other.
5. **Keys, token connectors and the repo.** Both sessions mount the same `.env`
   and token credentials and clone the same repo. A branch one session pushes is
   visible to the other.
6. **The thread's agent.** A handoff or a switch rebinds the thread for both.
7. **Feedback and support.** A down-vote or support request attaches the newest
   live session in the thread, whoever owns it, so the operator reading the
   report may be pointed at the other person's session.
8. **The turn queue.** Mentions are serialised per thread, so one person's turn
   waits for the other's. Queued messages stay grouped by author.

What does not cross: the session and transcript, the sandbox, the output
listing, carried work on replacement, OAuth grants, personal memory, the Daimon
MCP vault, notebook listing and deletion, the per-member cap and routines.
