# Teams adapter

The Teams adapter answers in 1:1 chats and in channel threads where it is
@mentioned. Registration steps live in
[teams-app-manifest.yaml](teams-app-manifest.yaml).

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
conversation tenant and the channel-data tenant both equal the configured one
and the sender has a well-formed `aad_object_id`. Users need no setup: their
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

### Commands and admins

Commands answer in the 1:1 chat only, since their replies can carry account
details; in a channel the bot points to the 1:1 chat. A message is a command
only when it is the bare word (or `memory /<path>`); anything longer goes to
the agent. None of them runs an agent turn.

| Command | Does |
| --- | --- |
| `new` | Start a fresh conversation, or end a setup conversation. Teams only; Slack and Discord ask the agent. |
| `help` | List the commands. |
| `setup` | Agents, their details and who answers where; create an agent, connect coding tools (admins), or open a setup conversation. |
| `routines` | List your routines (admins see all); admins create them, admins and creators pause, resume, read the last output or delete. |
| `memory` | Show what the 1:1 chat's agent remembers; add a path to read one file. |
| `privacy` | See, export or delete what daimon stores about you. |
| `billing` | Your usage this month; admins also see totals, top spenders and top-ups. No promo codes: admins redeem with the MCP tool `redeem_promo_code`. No channel budgets. |

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
There are no channel admins: only the listed admins administer a channel.

### Channel history

Like Discord and Slack, a channel turn replays the conversation it sits in,
read through Microsoft Graph and marked untrusted for the agent. The first
turn in a thread gets the root post and its newest 50 replies, a later turn
only the replies newer than the last message it read, and a mention that starts a
thread the channel's 25 most recently active posts. One page is read per turn,
marked `truncated` when there is more. System events, deleted posts and the
bot's own cards are left out; mentions read as `@name`, files as names.

Graph access is the resource-specific consent `ChannelMessage.Read.Group` in
the manifest. A team owner grants it when adding the app to a team, for that
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

MCP tools that need a chat platform work from Teams turns: `send_message` and
`create_thread` (text only, up to 6,000 characters, and only into a
conversation the requester belongs to), task handoff and fresh starts,
timers (`create_timer`, `list_timers`, `cancel_timer`), `bind_public_repo`
and the GitHub App install link. The MCP server posts
through the Bot Framework REST API with the same app registration.
With tool safety on (`DAIMON_TOOL_SAFETY__ENABLED`), an attached tool's write
waits on an Approve/Deny card in the conversation that only the requester can
answer.

### Capacity

Each tenant runs three turns at once by default
(`DAIMON_TEAMS__MAX_CONCURRENT_TURNS_PER_TENANT`). A new thread over the limit
gets a retry-later reply without starting a turn, and due timers and handoffs
from the wake poller wait until a turn finishes. A post or edit Teams throttles
(HTTP 429) is retried once after the wait Teams asks for, up to 10 seconds.

### Restart behaviour

On shutdown the adapter stops admitting, gives running turns 15 seconds, then
cancels them. The next boot edits every card a restart cut off to an
interrupted notice before admitting new turns. Teams cannot list a
conversation's messages, so a card whose send never returned an id cannot be
found and is left as is.

Handoffs, work waiting on a private input and timers are durable wake-queue
rows, and a wake poller opens every chat with due work. They survive a
restart, a timer fires at its time, and one whose turn had already started
when the process died is not run twice. A timer whose chat now answers as a
different agent posts a notice instead of running. A deployment with
`DAIMON_TEAMS__ENABLED=false` runs no timers.

### Not supported yet

Reactions, the agent's own channel-reading tools (`read_channel`,
`read_thread`, `search_messages`, `get_message`, `list_channels` and
`parse_link` are hidden from Teams turns), files in channels whose site is
not granted and in private or shared channels, and file posting
through `send_message`.
Also Discord and Slack only: `send_direct_message`, `/dm` conversations,
routine destinations (refused on save), table rendering
(`DAIMON_TABLE_RENDERING`) and completion pings (`DAIMON_COMPLETION_PINGS`).
Removing the app does not archive the organisation's tenant.
