# Slack

Connect Daimon to Slack workspaces, then learn how it behaves there: who it can
read, how files move and what shared channels allow. Read this if you run the
deployment or administer a workspace.

## Connect Slack

Slack needs a publicly reachable
[`DAIMON_MCP__PUBLIC_URL`](configuration.md#daimon_mcp__public_url). The bot
token comes from an OAuth install callback served by the `mcp` process, not
from an environment variable, and Slack will not redirect to `localhost`.

1. Create the Slack app from [`slack-app-manifest.yaml`](slack-app-manifest.yaml)
   and follow the steps in its header comment. It fills in the scopes, slash
   commands, events and Socket Mode toggles.
2. Put `DAIMON_SLACK__SIGNING_SECRET`, `DAIMON_SLACK__APP_TOKEN`,
   `DAIMON_SLACK__CLIENT_ID` and `DAIMON_SLACK__CLIENT_SECRET` in `.env`. Also
   set [`DAIMON_CRYPTO__KEYS`](configuration.md#daimon_crypto__keys): the
   adapter encrypts workspace tokens and refuses to start without it.
3. Start the adapter:

   ```bash
   docker compose --profile slack up --build -d
   ```

4. Open `https://<your-host>/oauth/slack/install` and install to a workspace.

After install, invite Daimon to a channel with `/invite @daimon` and mention
it. On a first install the installer also gets a short welcome DM.

### Update an existing app

New features need new scopes, events or commands. Apply the current manifest,
then reinstall the app so the workspace approves the scopes:

| Feature | Needs |
| --- | --- |
| `/github` command | The command from the manifest |
| Private conversations | `im:history` and `im:write` scopes, the `message.im` event, the App Home messages tab |
| Direct messages from agents | `im:write` scope |
| Slack Connect channels | The `channel_shared`, `channel_unshared` and `member_joined_channel` events |
| Channel lifecycle tracking | The `message.channels`, `message.groups` and channel archive, unarchive and delete events |
| File delivery | `files:write` scope |
| Agent names and avatars | `chat:write.customize` scope, requested at install when [agent identity](agent-identity.md) is on |

Slack file and DM errors tell the admin to reinstall when a scope is missing.
After adding `chat:write.customize`, restart the Slack and MCP services.

## How Daimon behaves in Slack

### Start conversations

A channel message starts a conversation only through `app_mention`. Follow-ups
in a thread need a real mention too. An admin can turn on plain-text mentions,
where a person types `@` and the bot's configured name. It ignores a name
followed by `(` or by a dot and a letter. A message with both counts once.

A mention that arrives while its thread is busy gets a waiting emoji and runs
after the active turn. Each participant in a thread keeps a separate session.
Tool approvals appear as cards in the thread, and only the requester can
approve or deny. Answers carry 👍 and 👎 buttons. A 👎 opens a short form for
reasons and text, and sends it to your support channel when
`DAIMON_SUPPORT__FEEDBACK_TO_SUPPORT` is on for the tenant.

### Read channels and history

By default Daimon reads only channels the bot is invited to. A top-level
mention starts a thread with no history, so the first turn gets the 25 messages
before the mention, marked untrusted. Apps outside the Marketplace get at most
15. A first turn inside a thread replays that thread. Slack may return fewer
messages than asked, and a failed or slow read (over 10 seconds) runs the turn
without history. Message tools return up to 200 messages per call, with a
cursor for older ones.

### Let members connect their Slack account

Members can connect their own Slack account. Daimon offers a link when it hits
a channel it cannot read. A connected member's reads then use their own Slack
permissions, with no bot invite needed, and include message search. Operators
should know:

- Daimon answers with channel content wherever the connected person asks,
  gated only by whether that person can see the source channel.
- Direct-message content is surfaced only in a DM with that person, never in a
  channel.
- User tokens (`xoxp-...`) are stored encrypted, one per person and workspace.
  **Disconnect Slack** in `/privacy` deletes and revokes them.
- Workspaces with admin app approval must approve the added user scopes first.

### Send and receive files

For Managed Agents turns, files in `/mnt/session/outputs` upload to the thread
after an interactive turn, a few seconds after the reply. Routines deliver
nothing. Files over 200 MB are not uploaded. The thread gets a notice naming
the file and offering a smaller version. Self-hosted turns skip this sweep, and
agents deliver files through the MCP message tools instead.

Read tools (`read_channel`, `read_thread`, `get_message`, `search_messages`)
list file names, types and sizes. Download links go through Daimon's file proxy
and expire after an hour, and private Slack URLs are never exposed. Without a
public MCP URL and signing secret, you get metadata only.

`send_message` accepts staged file handles and links from those tools. A link
is a bearer token, not proof of access. The file must be shared in a channel
the requester can view, a file from a sealed thread can be reposted only into
that thread, and a file shared only in a DM only into a DM. The limit is ten
files of 20 MiB each, and a caption is required.

A `.md` or `.zip` attached in a message can become a skill through
`add_skill`, after the person approves a confirmation card. The card appears
only when
[`DAIMON_TOOL_SAFETY__ENABLED`](configuration.md#daimon_tool_safety__enabled)
is on. Otherwise use the setup panel's **Add skill** form, which accepts pasted
text only.

### Work in shared channels

Slack Connect needs no setting. When Daimon is in a channel shared with another
workspace, that workspace's people can mention it. On every turn Daimon asks
Slack which workspace the person belongs to, and only a verified member of the
installing workspace counts as one of its own.

People from the other workspace are plain users, held to the channel they
asked in. They can stop their own turn, answer the approval card for it and
rate its answer. They cannot be admins, use private conversations, create
agents, set keys, read the directory, or use routines, billing, privacy, agent
setup, GitHub or **Ask a person**. Those controls reply "That's only for people
at your workspace".

Every turn in a shared channel is held to that channel, including your own
members' turns, and has no admin tools. Set agents up from an internal channel
or a DM. Channel-bound chat tools do not limit an agent's sandbox or
third-party connectors, so choose an agent whose connectors and repos suit the
other workspace. If both organisations install Daimon, the installation that
receives the event answers.

### Use private conversations

Workspace admins opt in with `/dm-me enable` and opt out with `/dm-me disable`.
In a channel, `/dm-me` continues privately with its recent text and a link
back. Later messages go straight to the app, and running `/dm-me` again resets
the scope. It carries recent channel text, not thread replies or attachments,
and it refuses in channels with limited readers.

Before enabling, finish the DM setup in the manifest: the two scopes, the
`message.im` event and the App Home messages tab. Without them people see
"Daimon can't use DMs in this workspace yet", and the log shows
`slack.dm.setup_incomplete`.

> **Warning:** Upgrade every MCP reader (session, agent-chat and hub
> endpoints) before enabling private conversations. Older readers ignore
> private session stamps and can expose private content to another caller on
> the same account. Keep DMs disabled through mixed-version rollouts and
> before any rollback.

Private turns start a fresh session each time. Recent private history is
replayed, but workspace files and OAuth grants held only in a shared vault are
not copied.

### Ask a person

Set `DAIMON_SUPPORT__SLACK_ESCALATION_CHANNEL_ID` to show an **Ask a person**
button beside 👍 and 👎. Each send spends one support credit, and sends one
request to that channel. The post carries the requester and a link, never
answer text. The bot must be in the channel. Leave it unset to hide the
button. External Slack Connect members cannot use it.

[`DAIMON_SUPPORT__CREDITS_PER_USER`](configuration.md#daimon_support__credits_per_user)
sets the credits, 20 per person per workspace by default and shared with
Discord. Zero turns the button off.

In a channel with its own admins, the request goes to them by DM first, then
to the server admins. The support channel gets it only if no DM landed.

Multi-platform deployments set one channel per platform: Discord uses
`DAIMON_SUPPORT__ESCALATION_CHANNEL_ID` and Teams its own channel. No request
crosses platforms. For several workspaces,
`DAIMON_SUPPORT__SLACK_ESCALATION_TEAM_ID` names the workspace that owns the
channel.

### Pick agents and see where they answer

`/agent-setup` opens the agents roster, and `/here` shows only the caller a
card with the agent that answers in the channel. Members see only agents whose
rules allow them to answer at that location, while admins see all. Knowing an
agent's name does not grant edit rights.

`/memory` shows an agent's shared memory, `/memory mine` your personal memory
with it, and `/memory forget-mine` clears yours after you confirm a code.
Replies are visible only to you. In a shared channel the agent uses that
channel's own memory instead. See [architecture](architecture.md) and
[permissions](permissions.md).

`/github` and `/github connect` open the GitHub flow described in
[GitHub](github.md). Completion pings, per-agent names and avatars, and table
rendering are optional. See
[`DAIMON_COMPLETION_PINGS`](configuration.md#daimon_completion_pings),
[agent identity](agent-identity.md) and
[`DAIMON_TABLE_RENDERING`](configuration.md#daimon_table_rendering).
