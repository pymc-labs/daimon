# Teams adapter

The Teams adapter answers in 1:1 chats and in channel threads where it is
@mentioned. Registration steps live in
[teams-app-manifest.yaml](teams-app-manifest.yaml).

### Ingress is HTTP, not a dial-out

Discord (gateway) and Slack (Socket Mode) dial out; Teams does not. The
adapter runs a FastAPI listener on `DAIMON_TEAMS__PORT` (default `3978`). The
Microsoft SDK owns `POST /api/messages` and validates the Bot Framework JWT
before daimon code runs; tokens from any other issuer (such as Entra ID) are
refused with 401 first. The same listener serves `/healthz` and `/readyz`.
`DAIMON_TEAMS__ENABLED=false` makes `/api/messages` answer 503, and bodies over
64 KiB are refused before parsing. The `teams` compose service (opt-in `teams`
profile) publishes the port; the messaging endpoint must reach it.

### Identity: one Entra organisation

A deployment serves one Entra organisation, `DAIMON_TEAMS__TENANT_ID`. At boot
the adapter provisions that tenant and reconciles its defaults; turns are
denied until the reconcile succeeds. The adapter fails closed unless the
conversation tenant and the channel-data tenant both equal the configured one
and the sender has a well-formed `aad_object_id`. Users need no setup: their
principal is created on first contact.

### Where it answers

- **1:1 chat.** Every message is a turn, in the organisation's tenant. It
  counts as a DM for the access policy, so `dm_memory_read_only` applies.
- **Channels.** Only messages that @mention the bot. Each root post is its own
  thread and session; replies that @mention it continue that thread.
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
| `routines` | List routines; admins create them, admins and creators pause, resume, read the last output or delete. |
| `memory` | Show what the 1:1 chat's agent remembers; add a path to read one file. |
| `privacy` | See, export or delete what daimon stores about you. |
| `billing` | Your usage this month; admins also see totals, top spenders and top-ups. |

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

### Files

- **In.** Images pasted into a message are fetched with the bot token and
  passed to the agent. Files shared in a 1:1 chat reach the agent as short-lived
  download links. The manifest must set `supportsFiles: true`.
- **Out.** Files the agent writes to its outputs are offered in the 1:1 chat
  with Teams' file consent card; accepting uploads the file to the user's
  OneDrive. Offers live in memory, so a restart drops them and the next turn
  that uses a tool offers the file again.
- **Channels.** Reading a file shared in a channel, or posting one, needs
  Microsoft Graph. The bot names a file it made there in a note and discards
  it.

The bot token is only sent to Bot Framework hosts, downloads and uploads only
go to SharePoint hosts, and every redirect hop is re-checked.

### Keys and sign-ins

When an agent asks for an API key or an MCP token (`request_agent_key`,
`request_mcp_token`), it posts a card in the conversation. Only the person who
asked can open it; the secret goes into a password field in a Teams dialog and
never through the chat. `request_mcp_oauth` opens a private sign-in link the
same way. Once the value is saved, the card shows the outcome and the waiting
work resumes. Replacing an existing key follows the same admin rules as Slack.

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
gets a retry-later reply without starting a turn.

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

Reactions, reading channel history (`read_channel`, `read_thread`,
`search_messages`, `get_message`, `list_channels` and `parse_link` are hidden
from Teams turns), files in channels (both need Microsoft Graph), file posting
through `send_message`, and private inputs a password field cannot take: `.env`
uploads, multi-line secrets, and repository or skill-repository tokens.
Also Discord and Slack only: `send_direct_message`, `/dm` conversations,
routine destinations (refused on save), table rendering
(`DAIMON_TABLE_RENDERING`) and completion pings (`DAIMON_COMPLETION_PINGS`).
Removing the app does not archive the organisation's tenant.
