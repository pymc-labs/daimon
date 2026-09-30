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

### Skill files

A `.md` or `.zip` attached in a message reaches `add_skill` as the file link
Daimon gave the turn. Daimon reads it with the bot token only when the link's
signature checks and it names this workspace, and refuses a file over the
skill size cap before downloading it. The setup panel's Add skill form takes a
paste only, since Slack modals have no file input.

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

Completion notifications can be enabled per tenant with `DAIMON_COMPLETION_PINGS`
(see [architecture](architecture.md#completion-signals)). Enabled turns post their
final answer as a fresh thread reply and mention only the requester. Trigger
messages replace admission eyes with a check on success; default tenants keep eyes.
Reaction permission errors do not fail turns.
Agent-initiated DMs use `send_direct_message` with a workspace user ID. Existing
installations need to reauthorize the app with the `im:write` bot scope; the
[app manifest](slack-app-manifest.yaml) includes it. The tool checks live workspace
membership, then opens a one-person DM and posts using the bot token.
See Slack's [conversations.open reference](https://docs.slack.dev/reference/methods/conversations.open/)
and the tenant [recipient policy](architecture.md#agent-initiated-direct-messages).

When a direct-message call fails because an install lacks `im:write`, the tool
asks a workspace admin to reinstall or reauthorize daimon from the install link.
Revoked, expired, or invalid bot authorizations give the same recovery direction.
Errors include the number of chunks already delivered; a failure to open the DM
sends no messages.
With the tenant enabled in `DAIMON_TABLE_RENDERING`, final-answer Markdown tables use native table blocks with wrapped cells, up to
20 columns and 100 rows including the header. Larger tables retain their Markdown
text. Surrounding prose and multiple tables are delivered in order.


### Private conversations

Workspace admins opt in with `/dm enable` (and disable with `/dm disable`). Then use
`/dm` in a channel to continue privately with its recent text context and a back-link.
Send later messages directly to the app. Run `/dm` again to reset the private scope.
Only current workspace members allowed by the tenant access policy can invoke it.

Update the app from `docs/slack-app-manifest.yaml` and reinstall it to grant the new
bot scopes `im:history` and `im:write`, subscribe to `message.im`, and enable the App
Home messages tab. Existing installations remain DM-disabled until an admin opts in.
This version moves recent channel text, not Slack thread replies or attachments.

Before `/dm` moves a conversation or `/dm enable` changes policy, daimon checks the
granted `x-oauth-scopes` for both IM scopes. Missing or unreadable grants refuse
without saving a route; scope/token errors request reinstall or reauthorization.
The scope header cannot verify event subscriptions: operators must also apply the
manifest's `message.im` subscription and enable the Messages tab.

Private turns use fresh MA sessions with isolated execution credentials. Recent
private history is replayed, but ephemeral workspace files are not carried forward.
External MCP OAuth grants held only in a shared vault are not copied into private
turn vaults; stored shared agent credentials still follow ordinary assembly.

Before enabling DM routing, upgrade **all MCP readers**, including session,
agent-chat and hub endpoints. Keep DMs disabled throughout mixed-version rollouts
and before rollback. Older readers ignore execution claims/private session stamps
and can expose private content to another caller on the same account. Do not roll
those reader guards back while private sessions remain: disabling routing does
not erase existing transcripts. Drain active turns before changing versions.
