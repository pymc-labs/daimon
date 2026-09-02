# Slack Adapter — Trust Model

This page documents how daimon's Slack adapter handles per-user access and
what operators should understand about the resulting trust model.

### Session output files

Files an agent saves under `/mnt/session/outputs` are uploaded to the Slack
thread after an interactive turn completes (scheduled routines deliver
nothing). Delivery runs in the background after the reply is posted — the
adapter waits for Managed Agents to index newly written files, so files may
arrive a few seconds after the reply. A successfully posted file is deleted
from the session's file listing, so the listing only ever holds undelivered
work and there is no delivery-receipt store to go stale. An interrupted
delivery (a deploy restart mid-sweep) leaves the file listed and it goes out
on the next turn; a crash between posting and deleting can re-post a file
once. Files over 20 MiB are not delivered — the thread gets a short notice
naming the file instead (Slack's own hard cap is far higher, but large files
lose thread previews and the upload buffers the whole payload in memory), and
0-byte files are skipped silently and logged. Delivery requires the
`files:write` bot scope; adding a scope to an existing install requires
re-running the install flow. A workspace that has hit its Slack file-storage
limit gets one in-thread notice and no deliveries until space is freed.

Agents can also post a file deliberately, mid-turn, through the MCP
`send_message` tool: `file_handles` for a file the agent produced, or
`attachments` with a file link from one of the read tools to re-post a file
already in the workspace. The text posts first and the files follow as
replies under it, so a file always has a caption. This uses the same
`files:write` scope, and a workspace without it gets a tool error naming the
reinstall step instead of a silent drop.

### Per-user Slack access (optional)

By default daimon reads only channels the bot is invited to. Members can
additionally **connect their Slack account** (daimon nudges them once, and
offers a link whenever it hits a channel it can't read). A connected member's
reads run with *their* Slack permissions: any channel or DM they can see, no
bot invite needed, plus message search (results that come from a DM are only
surfaced when you ask in a DM with daimon).

Trust model notes for operators:

- Connected users' reach is no longer signalled by bot presence in a channel.
  daimon answers with channel content wherever the connected user asks, gated
  only by whether that user can see the source channel themselves.
- User tokens (`xoxp-…`) are stored Fernet-encrypted (`DAIMON_CRYPTO__KEYS`),
  one row per (workspace, user), and are deleted + revoked from the `/privacy`
  panel ("Disconnect Slack").
- Workspaces with admin app-approval must have an admin approve the added
  user scopes before members can connect.
- Reads mirror the connecting user's own Slack visibility: any channel or DM
  they can see, answered wherever they ask — the same model as the Discord
  bot. The one exception is direct-message content (DMs and group DMs), which
  daimon will only surface in a DM with you, never in a channel.
