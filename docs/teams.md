# Teams adapter

daimon on Microsoft Teams answers in 1:1 chats and in channel threads where
it is @mentioned. It reads the thread it is asked about, works with files,
runs routines, and has the same agents, memory and billing as on Discord and
Slack. This page covers how it behaves and where it differs. To set it up
(Entra, Azure Bot, the app package and the Teams admin center), follow
[Microsoft Teams in the self-hosting guide](self-hosting.md#microsoft-teams-optional).

### Ingress is HTTP, not a dial-out

Discord (gateway) and Slack (Socket Mode) dial out; Teams does not. The
adapter runs a FastAPI listener on `DAIMON_TEAMS__PORT` (default `3978`). The
Microsoft SDK owns `POST /api/messages` and validates the Bot Framework JWT
before daimon code runs; tokens from any other issuer (such as Entra ID) are
refused with 401 first. The same listener serves `/healthz` and `/readyz`,
and, with `DAIMON_TEAMS__PUBLIC_URL` set, `GET /oauth/teams/files/callback`
(below); a reverse proxy in front must pass that path too.
`DAIMON_TEAMS__ENABLED=false` makes `/api/messages` answer 503, and bodies over
64 KiB are refused before parsing. The `teams` compose service (opt-in `teams`
profile) publishes the port; the messaging endpoint must reach it.

### Identity: one Entra organisation

A deployment serves one Entra organisation, `DAIMON_TEAMS__TENANT_ID`. At boot
the adapter provisions that tenant and reconciles its defaults; turns are
denied until the reconcile succeeds. The adapter fails closed unless the
conversation tenant equals the configured one and the sender has a well-formed
`aad_object_id`; in a 1:1 chat the channel-data tenant must match too. A
channel sender from another tenant, or a guest, is from another organisation (below). Users need no setup: their
principal is created on first contact. Only the commercial Microsoft 365 cloud
is supported; government and China clouds use other Bot Framework hosts.

### Where it answers

- **1:1 chat.** Every message is a turn, in the organisation's tenant. It
  counts as a DM for the access policy, so `dm_memory_read_only` applies.
- **Channels.** Messages that @mention the bot or quote one of its messages
  (Reply on a bot message); the quote reaches the agent in place. Each root
  post is its own thread and session; replies that address it continue that
  thread. A bare @mention asks about the thread; if the thread cannot be
  read, the bot says so instead of starting a turn. A followed thread also
  gets unprompted replies (below).
- **Private and shared channels** work like standard ones once the app is in
  them. The package declares `supportsChannelFeatures: tier1` (manifest
  1.25), but adding the app to a team does not add it to these channels: each
  one's owner adds it from the channel.
- **Group chats** get a short refusal.
- **Protected channels** (tenant access policy; ids look like
  `19:…@thread.tacv2`) and their threads get no reply, notice or tool post.
  One thread is named by adding `;messageid=<root post id>`. In a sealed
  channel or thread, the agent's memory is read-only.

A turn shows one status card, edited in place, with a Cancel button only the
author can use. The answer replaces the card, split across messages when long,
with Teams' thumbs up/down feedback on the last one. Messages sent while a turn
runs are queued and run as one follow-up per author. Work an agent hands off
(`hand_off_task`) runs right after the turn that queued it; if admission
refuses that work, it is dropped without a notice, as on Slack. Retried
deliveries are deduplicated in memory only, so a retry that lands after a
restart runs again.

### People from another organisation

Two kinds of people count as from another organisation: a shared channel's
external participants, who join through B2B direct connect and stay in their
home tenant, and guests (Entra B2B guest accounts in ours), who can be in
standard and private channels and 1:1 chats. daimon answers them only inside
a confidential channel and its threads. Anywhere else an addressed message gets
one line saying so and runs no turn, and their unmentioned replies are never
judged for a followed thread. Their turns run as a member (never an admin or
a channel admin, whatever is configured, a grant naming a team they own
included), commands go to the agent as plain text, and the agent sees
`external="true"` on their message. Their tool calls get only the
conversation's tools: reading, searching and posting in it, files into it,
their own sessions and timers, and signing in to an MCP server the agent
already has. Anything else, including tools added later, returns a tool
error the agent relays. Files can't be saved in a shared channel.

A sender is placed from these signals, cheapest first: a foreign tenant on
the activity (`channelData.tenant.id`, `from.tenantId`,
`from.properties.tenantId`); their Bot Framework roster entry (`tenantId`,
and `userRole` "guest"); then, in a channel, Graph's member list
(`allMembers`: `tenantId` and the "guest" role), which needs the
`ChannelMember.Read.Group` permission in the manifest. A foreign tenant is
external; a guest is too, unless the tenant access policy lists them as a
member (`daimon tenants access-policy --add-member-guest <object id>`). The
list never overrides a foreign tenant. Our tenant with a member's role is
internal.

Without either, the sender is unknown and nothing is stored. In a channel
not known to be standard or private (the activity, an earlier activity
there or the team's channel list says) they are held as external for that
turn. Elsewhere they are answered as before, unless the account is already
marked external. Positive evidence is stored on the account, so queued work,
routines and MCP tokens see it; a held turn's MCP calls are held too.
Answers are cached in memory (an hour for an external, ten minutes for ours,
a minute after a failure) and each lookup times out after a few seconds.

`DAIMON_TEAMS__RESTRICT_GUESTS` and `DAIMON_TEAMS__RESTRICT_EXTERNAL_PARTICIPANTS`
(both on) switch these rules off per kind: those people are then treated as
members, and the lookups only their kind needs are skipped.

### Commands and admins

Commands answer in the 1:1 chat, since their replies can carry account
details and Teams has no message only its sender sees. One typed in a channel
is answered in the sender's 1:1 chat, opened if needed, with a short pointer
in the channel; `new` there says each post is its own conversation. A message
is a command only when it is the bare word (or `memory /<path>`); anything
longer goes to the agent. None of them runs an agent turn.

| Command | Does |
| --- | --- |
| `new` | Start a fresh conversation, or end a setup conversation. Teams only; Slack and Discord ask the agent. |
| `help` | List the commands. |
| `setup` | Agents, their details and who answers where; create an agent, connect coding tools (admins, or a channel admin for a token bound to one of their channels), mint, list and revoke operator tokens (admins), or open a setup conversation. |
| `routines` | List your routines (admins see all); admins create them, admins and creators pause, resume, read the last output or delete. |
| `memory` | Show what the 1:1 chat's agent remembers; add a path to read one file. |
| `privacy` | See, export or delete what daimon stores about you. |
| `billing` | Your usage this month and recent credit grants; admins also see totals, top spenders and top-ups, and redeem promo codes. |
| `support` | Ask a person for help: a form whose Send spends one of your support credits. Listed only when `DAIMON_SUPPORT__ESCALATION_CHANNEL_ID` names a Teams channel (`19:…`) or, with the Discord bot configured, a Discord one. The post links to where it was asked. |

A 1:1 chat has no threads, so **Manage** in `setup` switches the chat into a
setup conversation for the chosen agent, with its own session. The chat's
usual session resumes when `new` or **End** closes it. Opening one runs no turn.

Teams has no workspace-admin flag a bot can read, so admins are listed by
Entra object ID in `DAIMON_TEAMS__ADMIN_USER_IDS`. Their turns run with the
admin role, and the panels unlock the admin actions for them. Every card click
and dialog re-checks the organisation, the clicker and their role. The list
is read at boot, which also takes the stored admin role, used by routines and
MCP clients, from anyone no longer on it. There are no ephemeral messages:
refusals come as toasts, dialog messages or card edits only the clicker sees.
Channel admins (`set_channel_admins`, by Entra object ID, or by a team's Entra
group ID in `role_ids` to admit that team's owners), channel budgets
(`set_channel_budget`) and channel environments (`set_channel_environment`)
work as on Discord and Slack. Who answers where lists each channel's
environment, and its **Channel settings** dialog changes one channel picked
there, since the panel lives in the 1:1 chat: its environment (server admins,
or that channel's admins), and whether it is confidential and its admins by Entra object ID
(server admins only).
Confidential channels work as on Discord and Slack, with `set_channel_isolation`,
`daimon channels isolate` or `--isolated-channel`. A thread (`;messageid=`)
counts as its channel, and the confidential channel's agents send nothing to 1:1 chats. The
CLI can't read channel names, so a copy it makes is named from the channel id.
The setup panel lives in the 1:1 chat, outside every channel, so a member's
Agents list leaves out each confidential channel's own agents; an admin sees all.

### Channel history

Like Discord and Slack, a channel turn replays the conversation it sits in,
read through Microsoft Graph and marked untrusted for the agent. The first
turn in a thread gets the root post and its newest 50 replies, a later turn
only the replies newer than the last message it read, and a mention that starts a
thread the channel's 25 most recently active posts, each with its newest 10
replies. One page is read per turn, marked `truncated` when there is more.
Each message carries its sender and time, and the turn names its channel. System events, deleted
posts and the bot's own cards are left out; mentions read as `@name`. The
newest 4 images in the replay are passed to the agent, and up to 10 shared
files get a download link where channel files work (below). When Graph cannot
be read, the agent is told history is unavailable and why, rather than
guessing from nothing.

Graph access is the resource-specific consent `ChannelMessage.Read.Group` in
the manifest, plus `TeamMember.Read.Group` for the owner list a team grant of
channel admins reads (cached for a minute; without it the team's owners are
not channel admins). A team owner grants them when adding the app to a team, for that
team only; no tenant-wide permission or admin consent is needed. It also makes
Teams deliver every channel post to the bot, which ignores those without a
mention unless their thread is followed. An existing install needs the updated app package uploaded again
(bump `version`), then accepting the permission when the team updates the
app. Tenant admins can turn this consent off
(`Set-MgBetaTeamRscConfiguration -State DisabledForAllApps`); the bot then
answers without history. A refused, throttled or slow read (10 seconds) never
fails a turn: it runs without history and the adapter logs one warning with
the HTTP status and no content.

### Following threads

As on Discord, a thread can be followed: the bot reads replies nobody
addressed to it and joins in when a small classifier says it can help. Ask
the agent ("follow this thread", "stop following"); it calls
`set_thread_participation`, and `get_thread_participation` says what applies.
Anyone in a channel can follow its threads; a whole channel or the
organisation needs a listed admin. The deployment default is
`DAIMON_THREAD_PARTICIPATION__MODE`; `disabled` turns it off for Teams and
Discord alike.

A burst of replies is judged once, after the thread has been quiet for
`QUIET_SECONDS`, over the thread read through Graph, and capped per thread
per hour. The turn runs as the burst's newest author and passes the same
admission and billing gates as a mention, but posts only its answer: no
status card or Cancel button, and every refusal, notice and error is only
logged. No turn posts a status message of its own under its answer, as on
Slack and Discord: what the person must hear rides on the answer, and only
cards (forms, file offers) are sent besides it. Root posts are never judged, the bot's own and other bots' messages
are ignored, and protected channels are skipped. Without Graph history (the
consent above) a followed thread stays mention-only.

### Files

- **In.** Images pasted into a message are fetched with the bot token and
  passed to the agent. Files shared in a 1:1 chat reach the agent as short-lived
  download links. The manifest must set `supportsFiles: true`.
- **Out.** Files the agent writes to its outputs are offered in the 1:1 chat
  with Teams' file consent card (the agent guidance describes this path); accepting uploads the file to the user's
  OneDrive. Offers live in memory, so a restart drops them and the next turn
  that uses a tool offers the file again.
- **Channels.** Teams sends the bot only a channel message's text, so the bot
  reads each message it answers from Graph and passes its images to the agent. Files live in the team's SharePoint site, which no
  team-scoped permission reaches: they work only in teams whose site an admin
  granted (below). There, a shared file from that site (never another one)
  reaches the agent as a short-lived download link, and each file the agent
  writes is uploaded to the channel's Files tab (never overwriting) and linked
  below its answer, or in one message when the answer has no room (never
  after an unprompted answer); a failed upload is only logged.
  Elsewhere, or when Graph refuses, the agent is told the shared file's name
  and why it could not be opened, for its answer to explain, and an output is logged
  (`teams.channel_output.skipped` or `.upload_failed`, no name or content) and
  dropped from the delivery listing; the agent's own copy stays in its
  workspace. The turn context tells the agent which case applies
  (`files="available"` or `"unavailable"`), learned per channel from the last
  folder lookup or upload and rechecked every 10 minutes while unavailable.

The bot token is only sent to Bot Framework hosts, the Graph token only to
`graph.microsoft.com`, downloads and uploads only go to SharePoint hosts, and
every redirect hop is re-checked (Graph reads follow none).

A shared `.md` or `.zip` can become a skill: `add_skill(attachment_url=…)`
takes its download link only over https from a SharePoint, OneDrive or Graph
host, sends no token, refuses a redirect off those hosts, and checks the
file's name before reading its capped body, with the usual preview and
confirmation card.

### Channel files (optional)

Grant the app `Sites.Selected`, which reaches only the sites granted to it,
then grant each team's site. The manifest does not change and no restart is
needed.

1. Entra portal → App registrations → the bot's app → API permissions → Add
   a permission → Microsoft Graph → Application permissions →
   `Sites.Selected` → Grant admin consent.
2. Set `DAIMON_TEAMS__PUBLIC_URL` to the Teams service's public base URL
   (the messaging endpoint without `/api/messages`). Same app →
   Authentication → Add a platform → Web → redirect URI
   `<DAIMON_TEAMS__PUBLIC_URL>/oauth/teams/files/callback`.
3. When a daimon admin shares a file in a team whose site is not granted,
   the bot posts an **Enable files** card (at most every 15 minutes per team).
   A SharePoint or global admin clicks it and signs in once (the first in the
   organisation must be a global admin, who consents for everyone); the Teams service
   then grants the app write on that team's site, and its next message sees
   the files.

Or grant a site by hand as a SharePoint or global admin holding
`Sites.FullControl.All`:

```http
GET https://graph.microsoft.com/v1.0/groups/{group-id}/sites/root
POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions

{"roles": ["write"],
 "grantedToIdentities": [{"application": {"id": "<DAIMON_TEAMS__CLIENT_ID>", "displayName": "daimon"}}]}
```

The group id is in the team's link (team → ⋯ → Get link to team → `groupId=`).

Standard channels only: private and shared channels keep files in a site of
their own, where outputs are not uploaded. Removing the site permission
(`DELETE /sites/{site-id}/permissions/{id}`) returns the team to names only.

### Keys and sign-ins

When an agent asks for an API key, a `.env` file, an MCP token or GitHub
access (`request_agent_key`, `request_mcp_token`, `request_repo_binding`,
`request_skill_repo_token`), it posts a card in the conversation. Only the
person who asked can open it; the secret goes into a Teams dialog and never
through the chat. Key values and `.env` contents take a multi-line field (paste
the file: a dialog has no file input), tokens a password field.
`request_mcp_oauth` opens a private sign-in link the same way. Once the value
is saved, the card shows the outcome and the waiting work resumes. Key names,
replacing a key, whole-file imports, and binding a repo or importing skills
onto a shared agent follow Slack's rules. A GitHub token that cannot read the
repo is refused in the dialog, and the form stays open.

### Agent tools

MCP tools that need a chat platform work from Teams turns, posting through
the Bot Framework REST API and reading through Graph with the same app
registration:

- **Channel reads.** `list_channels`, `read_channel`, `read_thread`,
  `get_message`, `list_threads`, `parse_link` (Teams message links) and
  `search_messages`, over the consent above, so "summarise this channel"
  works. The caller must be on the channel's roster; a private or shared
  channel is read only from a turn inside it. Graph has no message search for
  an app, so `search_messages` scans recent posts, bounded, and says when it
  stopped short. A 1:1 chat cannot be read back.
- **Posting.** `send_message` and `create_thread` (up to 6,000 characters,
  only into a conversation the requester belongs to). Files ride along: in a
  channel whose site is granted they are saved to its Files tab and linked; in
  a 1:1 chat they are offered with a file consent card. An agent can edit or
  delete what it posted (`edit_message`, `delete_message`); closing a thread
  (`delete_thread`, `archive_thread`) is not on Teams.
- **`send_direct_message`** opens a 1:1 chat with someone who shares a team
  with the caller and daimon, named by Entra object ID.
- **`post_wizard`** posts its form as an Adaptive Card that only the asker can
  fill in (no step images); Submit starts their turn.
- Task handoff and fresh starts, timers (`create_timer`, `list_timers`,
  `cancel_timer`), routines that post to a channel or thread (below),
  `bind_public_repo` and the GitHub App install link. A handoff is asked of
  the agent; the Hand over button that Discord and Slack show when a channel's
  agent changed under a thread is not on Teams, where the notice says to start
  a new conversation.

With tool safety on (`DAIMON_TOOL_SAFETY__ENABLED`), an attached tool's write
waits on an Approve/Deny card in the conversation that only the requester can
answer.

Nothing app-only lists the teams an app is in, so the adapter records each
team when the app is added or anyone writes there; an older install shows up
once someone writes in it. Adding the app posts a welcome (in the team's General
channel, or the 1:1 chat); removing it from a team forgets that team.

### Routines, pings and tables

A routine's destination can be a channel or a thread
(`<channel>;messageid=<root>`). Its creator must still be on the channel's
roster; when they are not, or the post fails, the result goes to their 1:1
chat if the direct-message policy allows. With completion pings on
(`DAIMON_COMPLETION_PINGS`), the answer is posted as a new message (in a
channel, @mentioning the asker) so Teams notifies, and the card then reads
"Done. The answer is below."; bots cannot react. Teams renders markdown tables
natively, so `DAIMON_TABLE_RENDERING` has nothing to do here.

### Capacity

Each tenant runs three turns at once by default
(`DAIMON_TEAMS__MAX_CONCURRENT_TURNS_PER_TENANT`). A new thread over the limit
gets a retry-later reply without starting a turn, and due timers and handoffs
from the wake poller wait until a turn finishes. A post or edit Teams throttles
(HTTP 429) is retried once after the wait Teams asks for, up to 10 seconds.

### Restart behaviour

On shutdown the adapter stops admitting, gives running turns 15 seconds, then
cancels them. The next boot edits every card a restart cut off to an
interrupted notice before admitting new turns. A channel card whose send never
returned an id is found by reading its thread through Graph; one in a 1:1 chat,
which cannot be read back, is left as is.

Handoffs, work waiting on a private input and timers are durable wake-queue
rows, and a wake poller opens every chat with due work. They survive a
restart, a timer fires at its time, and one whose turn had already started
when the process died is not run twice. A timer whose chat now answers as a
different agent posts a notice instead of running. A deployment with
`DAIMON_TEAMS__ENABLED=false` runs no timers.

### Not supported

Group chats, reactions, `/dm` conversations, files in channels whose site is
not granted or in private and shared channels. Removing the app does not
archive the organisation's tenant: a deployment serves one organisation.
