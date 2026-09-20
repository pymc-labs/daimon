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

`/agent-setup` opens the Agents roster, which pushes into either an agent's Details view or Who answers where. Setup conversations can be opened with **⚙️ Manage agents** from the Agents roster or from Details; creating a new agent lands on its Details view rather than a separate confirmation screen, and Details also offers **🧰 Use from your coding tools** to connect that agent over MCP. The channel gets a short launcher with a **Reply to Daimon** button; Daimon's welcome appears inside the shared thread. The panel also provides the reply button immediately after opening setup. Follow it, reply in that thread, and mention the bot. Daimon answers while the named agent is configured. Opening setup does not run a billed turn or change channel defaults. Each participant keeps a separate session.

Existing Slack apps must update **Event Subscriptions → Subscribe to bot events** to match `docs/slack-app-manifest.yaml`, including `message.channels`, `message.groups`, channel/group archive, unarchive, and deletion events. These subscriptions track setup lifecycle only; messages still trigger conversation only through `app_mention`. Root deletion is delivered as the [`message_deleted` message subtype](https://docs.slack.dev/reference/events/message/message_deleted/).
