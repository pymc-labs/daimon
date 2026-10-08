# Slack Adapter — Trust Model

This page documents how daimon's Slack adapter handles per-user access and
what operators should understand about the resulting trust model.

### `/here` card

`/here` sends only the caller a compact channel card. Its title names the agent
that answers here or the reason replies are unavailable. When an agent answers,
Reading and Publishing show the effective scope and whether approval is
required; a card that only gives a reason has no fields. The footer says
`Channel setting. Threads can differ.` because Slack slash commands have no
thread context. Credential names and values are absent from the card.

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
re-running the install flow. Channel admin grants that name a user group need
the `usergroups:read` bot scope; without it the form can't list groups and a
group grant admits nobody. A user group grant makes anyone who can join or edit
that group a channel admin, and by default every Slack member can edit user
groups, so limit user group management to admins in the workspace settings
before naming one. Outside a chat turn (MCP calls, the hub, channel admin
DMs) a group counts only while a fresh lookup, cached for a minute, still lists
the person. A workspace that has hit its Slack file-storage
limit gets one in-thread notice and no deliveries until space is freed.

### Files in message tools

`read_channel`, `read_thread`, `get_message` and `search_messages` include attached
file names, MIME types and sizes. Download URLs are signed and use daimon's file
proxy; private Slack download URLs are never exposed. Anyone holding a URL can
download the file, so these expire after an hour, with the turn grant, rather than
the 24 hours a turn's own attachment links last. Without a configured public MCP
URL and signing secret, metadata is still returned with no download URL. Deleted
and retention-hidden placeholders are omitted.

`send_message` accepts staged `file_handles` and signed file links from those read
tools in `attachments`. Links must be valid and belong to the same workspace;
arbitrary URLs are refused. A link is a bearer token, so it is not proof the
requester may see the file: Slack's list of where the file is shared must include
a channel the requester can view and the channel policy lets the call read. A file
in a sealed thread can be reposted only into that same thread, since
`send_message` carries no turn origin, and a file shared only in a 1:1 DM can be
reposted only into a DM. The combined limit is ten files, and each
file is capped at 20 MiB. A caption is required. The caption posts
first; files upload into its new thread or the existing target thread. The upload
messages are recorded for tidying when Slack reports their share in the upload
response. An upload failure leaves the caption posted, and the error
says not to send it again. The bot needs `files:write`; reinstall only when Slack
reports the installed token lacks it.

### History pages

Thread context requests one page of `DAIMON_SLACK__HISTORY_PAGE_LIMIT` messages
(default 100, range 1–1000). Slack may grant fewer depending on the app's rate
limits; `has_more` still marks the replay as truncated. Message tools return at
most 200 messages per call. `read_channel` returns a cursor for older messages.
`read_thread` returns the root and the newest replies, with a cursor for older
ones; the root counts toward its 200, and a limit below 2 still reads the root
and one reply. No automatic multi-page sweep runs during a turn.

A top-level mention starts a new thread with no history of its own, so its
first turn gets the channel instead: one `conversations.history` page of 25
messages ending at the mention, the mention itself left out, shown oldest
first in a `<channel_context source="slack" trust="untrusted">` block. Nothing
posted after the mention is included; earlier answers from the bot account
are. A first turn inside an existing thread replays that thread as before.

The channel is read as `read_channel` would read it for that turn: the
answering agent must be allowed to read it from the new thread, and a thread
whose readers are limited on its own is withheld (its root and broadcast
replies) before any file link is minted.

`truncated="true"` says older messages exist; apps distributed outside the
Marketplace get at most 15. When the access policy can't be read, the read is
refused, or Slack fails, rate-limits or takes over 10 seconds, the block is
`status="unavailable"` and the turn runs without it; the fetch never waits out
a rate limit. A recovery re-seed rebuilds the same window on the policy as it
is then. On an install held to one history call a minute, this fetch spends
that call, so a `read_channel` the agent makes straight after may wait up to a
minute for Slack's limit to reset.

The replay leaves out the turn's own status card, which is posted before
history is read. Every other message stays, including earlier answers from the
bot account and other bots' posts. The turn's controls name the bot account
(`platform_user_id`, the `auth.test` user for the workspace) and its native
`<@U…>` mention as the responder, so a mention of the app addresses the agent
answering, whatever its name. The bot account can front different agents over a
thread's life, so its earlier messages are not attributed to the current one.

### Skill files

A `.md` or `.zip` attached in a message reaches `add_skill` as the file link
Daimon gave the turn, or as a link from a read tool. Daimon reads it with the bot
token only when the link's signature checks, it names this workspace, and the file
passes the same sharing check as a repost: it is shared in a channel the caller can
view and this turn may read, or in a 1:1 DM only when the turn is in a DM. It refuses a file over the
skill size cap or not named `.md` or `.zip` before downloading it. Adding it
needs the person's Approve on a confirmation card, which only tool safety
(`DAIMON_TOOL_SAFETY__ENABLED=true`) shows, and only in a chat turn whose
session asks before `add_skill`. When no card can show, the preview says which
it is: approval cards are off for the deployment, or this conversation can't
show one and why (a missing or stale `origin_context_id`, a session started
before tool safety was on, a session daimon could not read). Without the card, use the setup panel's Add skill form, which
takes a paste only, since Slack modals have no file input.

### Per-user Slack access (optional)

Tool approvals appear as Block Kit cards in the turn's thread. The card shows
the action, consequence and requester, with **Approve**, **Deny** and
**Details** buttons. Details sends a few plain labelled inputs privately to
the clicker. Only
the requester can approve or deny. Each call gets its own card. Answered,
expired and stopped cards lose
their buttons.

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

`/github connect` sends a workspace admin a private link for the agent answering
in that channel. Use `/github connect AgentName` to choose another agent. The
link lets the admin pick repos, then activates them for that agent. If the
agent still has a saved GitHub key, `/github connect` shows the **Update and
restart chats** confirmation instead. Existing Slack installations need the
`/github` command added from `docs/slack-app-manifest.yaml` and reinstallation.

Existing Slack apps must update **Event Subscriptions → Subscribe to bot events** to match `docs/slack-app-manifest.yaml`, including `message.channels`, `message.groups`, channel/group archive, unarchive, and deletion events. These subscriptions track setup lifecycle only; messages still trigger conversation only through `app_mention`. An `app_mention` runs a turn only when the message actually contains `@daimon`, so follow-ups in a thread need the mention too; Slack has been reported to deliver the event for un-mentioned thread replies, and those are dropped. Root deletion is delivered as the [`message_deleted` message subtype](https://docs.slack.dev/reference/events/message/message_deleted/).

Completion notifications can be enabled per tenant with `DAIMON_COMPLETION_PINGS`
(see [architecture](architecture.md#completion-signals)). Enabled turns post their
final answer as a fresh thread reply and mention only the requester. Trigger
messages replace admission eyes with a check on success; default tenants keep eyes.
Reaction permission errors do not fail turns.
A mention that arrives while its thread is busy gets ⌛ and waits for the
active turn. The ⌛ comes off once that request is answered, fails, is cancelled
or is dropped unanswered.
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

Final answers go out as `markdown` blocks. Slack shows text inside inline code
and fenced blocks exactly as written, entities included, so daimon sends code
unescaped and escapes only the prose around it: `<https://example.com|label>`
in prose becomes a link, while the same text in code stays literal. Prose keeps
user and channel mentions but not `<!channel>`, `<!here>` or `<!everyone>`. Each
message is checked again as sent, since splitting a long answer can leave code
lines outside their fence. Where the code boundary is ambiguous (an inline span
across lines, a fence inside a blockquote, a span in a table cell holding `|`,
a backtick a link or bare URL could take), the text is escaped as prose and its
`<` shows as `&lt;`. The message's `text` field, which Slack parses as mrkdwn
for notifications, escapes code as well.


### Feedback on answers

Every final answer carries 👍 and 👎 buttons on its last message. A turn that only
ran tools (a file written, a chart posted) carries them on its finished status card.
👍 records the vote and thanks the person with a message only they see.

👎 records the vote and opens a **What went wrong?** form. The form offers optional
reasons (wrong or inaccurate, didn't do what I asked, incomplete or cut off, too slow,
something else) and optional free text, and needs at least one. Every 👎 click opens
it, so a person can come back and add details. If Slack doesn't open the form, they
get a private **Tell us what went wrong** button that opens it. Sending the form
records a down-vote on that answer with the reasons and text. Each person has one
row per answer, so a double click or a second form replaces the first rather than
adding another.

Who may vote: anyone who could start a turn there (the invoker allowlist and the
channel's rules), checked when the vote is recorded and again on send. Anyone else is
told they can't, and nothing is recorded. External Slack Connect members are refused.
The text is stored on the feedback row. Logs carry the row id and the reason
codes. Deleting your data with `/privacy` removes the row.

To also send 👎 forms to the support team, turn on
`DAIMON_SUPPORT__FEEDBACK_TO_SUPPORT` for the workspace's tenant (a JSON object of
tenant UUID to `true`; off by default). Each sent form is then posted once to the
**Ask the team** channel (`DAIMON_SUPPORT__SLACK_ESCALATION_CHANNEL_ID`), with the
person, the agent and session, the reasons, the text and a link to the answer, never
its content. Sending the same form again posts nothing; a changed one posts again.
The form says it is shared before it is sent. A sealed origin is marked, as Ask the
team marks it. No support request is spent. Discord does the same with its 👎 text,
to `DAIMON_SUPPORT__ESCALATION_CHANNEL_ID`.

Emoji reactions (a :-1: on the message) are not read: that would need the
`reactions:read` scope and a reinstall of every workspace.

### Ask a person

Set `DAIMON_SUPPORT__SLACK_ESCALATION_CHANNEL_ID` to show an **Ask a person** button
next to the 👍/👎 buttons on every final answer (and on a tool-only turn's status card). It opens a short form; sending it
spends one of the person's support requests (`DAIMON_SUPPORT__CREDITS_PER_USER`,
default 20, counted per person per workspace and shared with Discord's ledger) and
posts the request to that channel. The click shows a "Checking…" form at once and
then the note form, or the reason there is none. Opening the form spends nothing, and asking twice
on the same answer (a double click, two open forms, a Slack retry) records and posts
once.

- **Slack-only deployments** set the Slack channel. Leaving it unset hides the
  button: a request nobody reads is worse than none.
- **Discord + Slack deployments** set both channels. Discord (and Teams) requests go
  to `DAIMON_SUPPORT__ESCALATION_CHANNEL_ID`, Slack requests to the Slack channel; a
  Slack request is never posted to another platform, and setting only the Discord
  channel leaves Slack off. (Teams is the one cross-platform case: its `support`
  command may post to a Discord channel, see [Teams](teams.md).)
- **Several workspaces**: requests are posted with the requesting workspace's bot
  token unless `DAIMON_SUPPORT__SLACK_ESCALATION_TEAM_ID` names the workspace that
  owns the channel; then every workspace's requests are posted with that one's token.
  The bot must be a member of the channel.

- **Channels with their own admins** (Discord too): the request goes to those
  admins by DM instead, then to the server admins when none could be reached, and
  to the escalation channel only when no DM landed. The asker never gets their own
  request, and each DM follows the workspace's direct message policy. Recipients
  are matched by the roles or groups stored at their last turn; a Slack user group
  is looked up again first.

Who may ask: anyone who could start a turn in that thread (the invoker allowlist and
the channel's writers rule, checked on click and again on send). External Slack
Connect members are refused. An escalation channel whose rule lets nobody write
refuses the post; the request
stays recorded as undelivered.

What is posted: the requester (mention, username, user and workspace id), a link to
the answer, and their note. No answer text or conversation content, the same as
Discord, and link previews are off, so the link shows nothing to anyone who can't
already open the channel. When the answer is in a channel or thread with limited
readers, the form
tells the person their note leaves the channel, and the post says to answer there.

### Private conversations

Workspace admins opt in with `/dm enable` (and disable with `/dm disable`). Then use
`/dm` in a channel to continue privately with its recent text context and a back-link.
Send later messages directly to the app. Run `/dm` again to reset the private scope.
Only current workspace members allowed by the tenant access policy can invoke it.
`/dm` refuses in a channel with limited readers and leaves threads with their own
rule out of the copied history, so their content never moves into a DM.

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

### Agent names and avatars

This feature is off by default. Set `DAIMON_AGENT_IDENTITY__ENABLED=true`
and restart the Slack and MCP services to show per-agent headers and the
Avatar control. With it off, the app posts as itself and keeps agent names in
answer footers.

Each non-built-in agent posts turn messages with its own Slack message name and
avatar. The built-in Daimon agent keeps the app's name and icon. Files uploaded
by a turn still appear as the app. The avatar URL is public to anyone who sees
the message; cached copies can remain after an avatar is changed or deleted.

When identity is enabled, new OAuth installs request `chat:write.customize`;
with it off, they use the previous consent screen. The app needs that scope
to show agent headers. To add it, first open the
**staging** app at [api.slack.com/apps](https://api.slack.com/apps). Under
**OAuth & Permissions → Bot Token Scopes**, add `chat:write.customize`. Open
**Install App** and click **Reinstall to Workspace**, then approve the new
scope. Repeat for each staging workspace. Verify an agent's answer has its own
name and avatar before repeating these steps for the **production** app and
its workspaces. An installation without the scope keeps posting with the app
header until it is reinstalled.
Restart the Slack and MCP services after reinstalling to clear any remembered
missing-scope result immediately; otherwise the result expires within 15 minutes.

Workspace admins can open `/agent-setup`, select an agent, then use the Picture
row's **Change** button to upload one PNG, JPG, GIF, or WebP image (up to 2 MB).
The image is center-cropped to a 256×256 PNG. **Reset** restores the assigned
default Daimon face. The Picture control is hidden when identity is off. Each
change gets a new URL. Pictures are public: anyone who sees a message can open
its image, and platform caches can keep a copy after the picture changes.
Uploaded files stay in the uploader's Slack files until
that person removes them from Slack.
