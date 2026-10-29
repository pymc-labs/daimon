# Microsoft Teams

Connect Daimon to Microsoft Teams, then learn how it behaves in 1:1 chats,
channels and group chats. Read this if you run the deployment or administer a
Microsoft 365 organisation.

## Before you start

Teams differs from Discord and Slack in two ways:

- **Teams pushes messages to you.** Microsoft delivers each message to an HTTPS
  address you provide, so the service needs a public hostname with a trusted
  certificate. Discord and Slack dial out instead.
- **One deployment serves one organisation.** People from other organisations
  in its channels and group chats are answered as members.

You need:

- **A Microsoft 365 organisation with Teams, and a work account in it.** Use
  that account for every step. A personal account creates a separate, empty
  Azure directory.
- **An Azure subscription in the same organisation.** Pay-As-You-Go is enough.
- **A public HTTPS hostname** such as `teams.example.com`.
- **The right roles**, or someone who has them:

| Step | Who can do it |
| --- | --- |
| Register the app in Entra | Anyone, if users may register apps; otherwise Application Developer |
| Create the Azure Bot | Contributor or Owner on the subscription or resource group |
| Upload the app to Teams | A Teams administrator, or anyone if custom uploads are allowed |
| Turn on channel files (optional) | A global administrator or Privileged Role Administrator, to consent once |

## Connect Teams

### 1. Register the app in Entra

1. Go to [entra.microsoft.com](https://entra.microsoft.com), then **App
   registrations** > **New registration**.
2. Name it (`daimon` is fine), choose **Accounts in this organizational
   directory only**, leave the redirect URI empty and select **Register**.
3. Copy the **Application (client) ID** (`DAIMON_TEAMS__CLIENT_ID`) and the
   **Directory (tenant) ID** (`DAIMON_TEAMS__TENANT_ID`).
4. Under **Certificates & secrets**, select **New client secret**. Copy the
   **Value**, not the Secret ID, as `DAIMON_TEAMS__CLIENT_SECRET`. It is shown
   once. Note its expiry date: the bot stops answering when it lapses.

You need no API permissions yet. A team owner grants channel access when
adding the app, and a chat member grants group-chat access. Only channel files
and private-channel messages need an application permission, covered below.

### 2. Create the Azure Bot

1. In [portal.azure.com](https://portal.azure.com), create a resource group
   such as `daimon`.
2. Select **Create a resource**, search for **Azure Bot** and select **Create**.
   Choose pricing tier F0 (Free), type Single Tenant and creation type **Use
   existing app registration**. Paste the client ID and tenant ID.
3. Leave the messaging endpoint empty for now.
4. Open the bot > **Channels**. Under *Available channels*, select the name
   **Microsoft Teams**. It is a link, not a button. Accept the terms, choose
   **Microsoft Teams Commercial** and select **Apply**. Teams should show as
   *Healthy*.

If clicking does nothing, check **Access control (IAM)** > **View my access**
for read-only access, or run this in Cloud Shell:

```bash
az bot msteams create -g <resource-group> -n <bot-name>
```

### 3. Run the Teams service

Pick the admin team. Its owners are Daimon's admins, who create routines,
replace shared keys, top up and see everyone's usage. In Teams, open the team's more menu, select **Get link to team** and copy the
`groupId` parameter. To change admins, change the team's owners. A removed
owner loses admin within about a minute.

Add the values to `.env`:

```bash
DAIMON_TEAMS__CLIENT_ID=<application (client) ID>
DAIMON_TEAMS__TENANT_ID=<directory (tenant) ID>
DAIMON_TEAMS__CLIENT_SECRET=<client secret value>
DAIMON_TEAMS__ADMIN_TEAM_ID=<groupId of the admin team>
```

Set the credentials together: a half-filled `DAIMON_TEAMS__` block stops every
Daimon process.
Also set [`DAIMON_CRYPTO__KEYS`](configuration.md#daimon_crypto__keys). Then
start the service:

```bash
docker compose --profile teams up --build -d
```

It listens on `127.0.0.1:3978`
([`DAIMON_TEAMS__PORT`](configuration.md#daimon_teams__port)) and serves
`/api/messages`, `/healthz` and `/readyz`. `docker compose logs teams` should
show `teams.tenant_ready`.

Microsoft must reach `/api/messages` over HTTPS through a reverse proxy or
tunnel. With [Caddy](https://caddyserver.com):

```text
teams.example.com {
	@teams path /api/messages /healthz /readyz
	handle @teams {
		reverse_proxy 127.0.0.1:3978
	}
	handle {
		respond 404
	}
}
```

Only those paths need to be public, plus two Graph paths if you enable
private-channel messages. Check it with
`curl https://teams.example.com/healthz`, which returns `{"status":"live"}`.

### 4. Point the bot at Daimon

In the Azure Bot > **Settings** > **Configuration**, set the **Messaging
endpoint**, including the `/api/messages` suffix:

```text
https://teams.example.com/api/messages
```

A missing suffix is the most common reason a bot never answers. Check that
**Microsoft App ID** and **App Tenant ID** match step 1 and **App type** is
SingleTenant, then select **Apply**.

### 5. Build the app package

Teams installs apps from a zip with `manifest.json`, `color.png` (192x192,
full colour) and `outline.png` (32x32, white on transparent) at its top level.
The template is [`teams-app-manifest.yaml`](teams-app-manifest.yaml). From the
repository root, with Python 3 and PyYAML, fill in your client ID and hostname:

```bash
mkdir -p teams-app
CLIENT_ID=<application (client) ID> TEAMS_HOST=teams.example.com python3 - <<'EOF'
import json, os, yaml
text = open("docs/teams-app-manifest.yaml").read()
text = text.replace("${DAIMON_TEAMS__CLIENT_ID}", os.environ["CLIENT_ID"])
text = text.replace("DAIMON_HOST", os.environ["TEAMS_HOST"])
with open("teams-app/manifest.json", "w") as f:
    json.dump(yaml.safe_load(text), f, indent=2)
EOF
```

In `manifest.json`, point `termsOfUseUrl`, `websiteUrl` and `privacyUrl` at
pages you serve, since Daimon serves none. `name` is what people see. Give a
test deployment its own Entra app, Azure Bot and package, with a distinct name
such as `daimon (test)`.

Add the icons (`assets/icon.png` resized works for `color.png`), then zip the
three files, not their folder:

```bash
cd teams-app && zip ../daimon-teams.zip manifest.json color.png outline.png
```

### 6. Upload and allow the app

In the [Teams admin center](https://admin.teams.microsoft.com):

1. Select **Teams apps** > **Manage apps** > **Upload new app** and choose the
   zip.
2. Open the app > **Users and groups** > **Edit availability**, and make it
   available to its users. Without app-centric management, check that its
   status is **Allowed** and that **Permission policies** allow custom apps.

A new upload can take minutes to hours to appear. Users who may sideload can
use **Apps** > **Manage your apps** > **Upload an app** instead.

### 7. Say hello

- **1:1 chat.** Select **Apps** > **Built for your org** > your app > **Add**.
  Send `help` or `setup`.
- **A team.** On the app's page, open the menu beside **Add**, select **Add to a
  team** and pick a team and channel. A team owner must accept the permission to
  read channel messages. Then @mention the bot, choosing its name from the
  autocomplete list: typed text alone does not count. Add the app to the admin
  team too, or nobody is an admin.
- **A private or shared channel.** The channel's owner adds the app from the
  channel's own apps settings. Adding it to the team does not add it there.
- **A group chat.** Add the app from the chat's apps and allow
  `ChatMessage.Read.Chat` when asked.

### Work with people from another organisation (optional)

Use a Teams shared channel with B2B direct connect, as described in
Microsoft's guide to
[collaborating with external participants](https://learn.microsoft.com/en-us/microsoft-365/solutions/collaborate-teams-direct-connect).

1. In Entra, under **External Identities** > **Cross-tenant access settings**,
   add the other organisation in both tenants. Yours allows B2B direct connect
   inbound, theirs outbound. Changes can take up to six hours.
2. In the Teams admin center, under **Teams policies**, allow "Create shared
   channels" and "Invite external users to shared channels" for you, and "Join
   external shared channels" for them.
3. Keep guest access enabled in Teams, SharePoint and Microsoft 365 Groups.
4. Create the shared channel, optionally set a channel rule
   (`daimon channels rule set`), and add the other organisation's people.
   Guests cannot be added; someone with a guest account in your organisation
   is added as an external participant (search `ext:user@domain.com`).

### Turn on channel files (optional)

By default the bot reads images in channel messages and handles files in 1:1
chats. Channel files live in SharePoint, which team-level consent cannot reach.

1. In Entra, open your app > **API permissions** > **Add a permission** >
   **Microsoft Graph** > **Application permissions**, select
   `Files.ReadWrite.All` and **Grant admin consent**.
2. Open each new channel's Files tab once, so its folder exists.

No restart is needed, and each channel turns on within 10 minutes. The
permission reaches every file in the organisation, so Daimon opens and saves
files only inside the channel's own Files folder. A shared channel hosted by
another organisation keeps its files out of reach.

### Turn on private and shared channel messages (optional)

Teams sends the bot only private and shared channel posts that @mention it.
To make followed threads, replies to the bot and typed `@Daimon` work there:

1. Grant the Graph application permission `ChannelMessage.Read.All` with admin
   consent, as in the channel files steps. It is tenant-wide, but Daimon
   subscribes only to channels it joins or sees a message in.
2. Set `DAIMON_TEAMS__PUBLIC_URL` and
   [`DAIMON_PRIVATE_CHANNEL_MESSAGES__ENABLED=true`](configuration.md#daimon_private_channel_messages__enabled).
3. Add `/api/graph/notifications` and `/api/graph/lifecycle` to the proxy's
   path list.
4. Restart the service. Add the bot to each channel, or @mention it once where
   it already is.

Shared channels hosted by another organisation cannot be subscribed.

### If the bot doesn't answer

| Symptom | Cause and fix |
| --- | --- |
| Teams will not add the app and says to check the registration and Teams channel | The Azure Bot's Teams channel is off (step 2), or the manifest `id` and `botId` do not match the client ID. |
| No answer anywhere and nothing in `docker compose logs teams` | Messages are not reaching Daimon. Check the messaging endpoint and `/api/messages` suffix (step 4), and that `/healthz` answers from outside. |
| 1:1 chats work but channel @mentions do not | The app was added to the team before the endpoint was right. Remove it under team > **Manage team** > **Apps** and add it again. Also pick the mention from autocomplete. |
| A reply on a bot message gets no answer in a private channel | Teams sends only posts that @mention the bot. Mention it, or enable private-channel messages. |
| Every Daimon process fails at startup | A `DAIMON_TEAMS__` value is missing, or `DAIMON_TEAMS__ADMIN_TEAM_ID` is not a group ID. |
| Nobody is an admin | `DAIMON_TEAMS__ADMIN_TEAM_ID` is unset, or the app is not installed in that team. `teams.admin_team.lookup_failed` in the logs gives the reason. After a failed read the last owners count for up to an hour. |
| `teams.tenant_reconcile_failed` in the logs | Daimon could not finish setting up your organisation and refuses turns until it does. The log line says why. |
| It stopped answering after months | The client secret has probably expired. Create a new one (step 1), update `.env` and run `docker compose --profile teams up -d`, which also recreates `mcp`. |

### Update the app

Manifest changes reach Teams only in a new package. Bump `version` in
`manifest.json`, zip it again and upload it over the existing app in the Teams
admin center (the app > **Upload file**). A team owner may be asked to accept
new permissions. Rebuild packages made before template version 0.6.0 or
manifest 1.25, which lack `/` commands, group chats and private or shared channels.

## How Daimon behaves in Teams

Teams uses one bot identity for every agent. Answers from an agent other than
the built-in one start with its bold name. Agents, memory and billing match
Discord and Slack. Only the commercial Microsoft 365 cloud is supported.

### Where it answers

- **1:1 chat.** Every message is a turn.
- **Channels.** A message that @mentions the bot or replies to one of its
  messages. Each root post is its own thread and session. A followed thread
  also gets unprompted replies. Private and shared channels send only posts
  that mention the bot, unless you enabled private-channel messages.
- **Group chats.** One session per chat, answering a mention, reply or command
  while everyone in the chat is from your organisation. It never joins in
  unprompted, and everyone sees each answer.
- **Channels with `writers: none`.** No reply, notice or tool post.

A turn shows one status card with a Stop button, then the answer, then a card
with the summary line and feedback buttons. Messages sent during a turn queue
and run as one follow-up per author. **Ask a person** appears when
`DAIMON_SUPPORT__TEAMS_ESCALATION_CHANNEL_ID` names a Teams channel.

### Commands

In a channel or group chat, most commands answer privately in the thread, while
`setup` and `support` move to your 1:1 chat. A message is a command only when it
is the bare word, with or without a leading `/`. Anything longer goes to the
agent.

| Command | Does |
| --- | --- |
| `new` | Start a fresh conversation, or end a setup conversation. |
| `help` | List the commands that work where you sent it. |
| `setup` | Manage agents and who answers where; connect coding tools and mint operator tokens (admins). |
| `here` | Show who answers here, what it can read and whether publishing needs approval. |
| `routines` | List routines (admins see all); admins create, and admins and creators pause, resume or delete. |
| `memory` | Show what the agent remembers here. `memory mine` shows your personal memory, and `memory forget-mine` clears it after a code. |
| `privacy` | See, export or delete what Daimon stores about you, or disconnect your Microsoft account. |
| `billing` | Your usage and credit left. Admins also see spend, channel budgets and can add credit. |
| `support` | Ask a person, spending one support credit. |

Admins are the owners of the admin team, and nobody else. The app must be
installed in that team so Daimon can read its owners. Channel rules, budgets
and environments work as on Discord and Slack.

### Files

| Direction | Behaviour |
| --- | --- |
| In, 1:1 chat | Images and files reach the agent as short-lived download links. |
| In, channel | Images are read through Graph. A shared file reaches the agent only with channel files on and only from the channel's own Files folder. |
| Out, 1:1 chat | The bot offers each file with a "Save to your OneDrive?" consent card. |
| Out, channel | With channel files on, files go to the channel's Files tab and are linked under the answer, never overwriting. |
| Inline previews | PNG, JPEG, GIF and WebP outputs show inline, up to four, downscaled. Office and PDF outputs in channels show a first-page thumbnail. |
| Group chats | Files are skipped. |

The agent can save an edited channel file as a new version with
`replace_channel_file`. If SharePoint refuses (a locked file, or a retention
policy or label that blocks app writes), the agent posts a new file instead.

### Keys and sign-ins

When an agent needs an API key, `.env` file or MCP token, it posts a card that
only the asker can open. The secret goes into a Teams dialog, never the chat.
Paste `.env` contents into the multi-line field, since dialogs have no file
input. For an MCP sign-in the agent sends a private link the same way. Once
the value is saved, the card shows the outcome and waiting work resumes.

### Microsoft account (optional)

Each person can connect their own Microsoft 365 account, so the agent reads and
changes their files, mail, calendar and notes with exactly their permissions.
Register a single-tenant app in the same Entra tenant:

1. Add a **Web** redirect URI `<root>/oauth/microsoft/callback`, where `<root>`
   is [`DAIMON_MCP__PUBLIC_URL`](configuration.md#daimon_mcp__public_url)
   without the trailing `/mcp`, and create a client secret.
2. Under **API permissions**, add these Microsoft Graph delegated permissions:
   `openid`, `profile`, `User.Read`, `offline_access`, `Files.ReadWrite.All`,
   `Sites.Read.All`, `Mail.Read`, `Mail.Send`, `Calendars.ReadWrite` and
   `Notes.ReadWrite.All`. Grant admin consent, or let each person consent at
   sign-in.
3. Set `DAIMON_MICROSOFT_ACCOUNTS__CLIENT_ID`, `__CLIENT_SECRET` and
   `__TENANT_ID` (and `__AUTHORITY` for a national cloud), plus
   `DAIMON_CRYPTO__KEYS` and `DAIMON_MCP__PUBLIC_URL`. Use identical values on
   every service that runs turns and on the MCP server.

With none or only some set, the tools stay absent. When a tool needs an
account the person has not connected, the agent asks first. If they agree, a
single-use link, valid for ten minutes, goes to their 1:1 chat. They must sign
in as the same Entra user. If Conditional Access later demands a new sign-in, the saved
account is dropped and the agent asks again.

Mail, calendar and OneNote work only from the person's 1:1 chat. Sending mail
or an invite always waits for the person to approve a card showing the
recipients and message. Refresh tokens are stored encrypted. **Disconnect
Microsoft** under `privacy` deletes them but does not revoke Microsoft's own
sessions.

### Restart behaviour

On shutdown the adapter stops admitting turns and gives running turns 60
seconds, so give the container a 90-second stop grace (see
[operations](operations.md#deploy-without-dropping-turns)). Turns cut off by a
restart are marked interrupted: the next boot edits each of their cards to an
interrupted notice before admitting new turns.

Each incoming message runs once, even across restarts. A message still
waiting when the restart began is kept, and the next process runs it unless it
is over 10 minutes old, when the sender is asked to resend. A turn that had
started is never rerun. A message sent to a 1:1 chat during the gap is lost.

### Platform limits

| Limit | Value |
| --- | --- |
| Concurrent turns per tenant | 3 by default ([`DAIMON_TEAMS__MAX_CONCURRENT_TURNS_PER_TENANT`](configuration.md#daimon_teams__max_concurrent_turns_per_tenant)); a new thread over the limit gets a retry-later reply |
| Channel history on a first turn | Root post plus the newest 50 replies; 25 recent posts for a channel-level mention; newest 4 images |
| Group chat history | Newest 25 messages first, then only newer ones |
| Graph read timeout | 10 seconds; a slow read runs the turn without history |
| Channel-file upload | Outputs capped at 20 MiB |
| Pasted skill text | 4,000 characters |
| Rate limits | A throttled post is retried once, up to 10 seconds |
| Not available | `/dm-me`, files, routines and agent tools in group chats; files in channels whose site is not granted; Teams-hosted files of another organisation |

Channel history needs the consents `ChannelMessage.Read.Group` and
`TeamMember.Read.Group`, which a team owner grants when adding the app. Without
them the bot answers without history.
