# Self-hosting daimon

This guide expands the README quickstart. It covers the full Docker Compose
setup, running the processes by hand, Slack, Microsoft Teams, the Claude Code
login mounts, chart storage and connecting MCP servers.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with Compose.
- An Anthropic API key **in a workspace dedicated to this deployment**.
  daimon manages the workspace's Managed Agents resources as its own, so
  sharing the workspace with anything else causes collisions.

## 1. Configure the environment

```bash
cp .env.example .env
```

Open `.env`, then uncomment and fill in:

- `DAIMON_ANTHROPIC__API_KEY`: your Anthropic API key.
- `DAIMON_MCP__JWT_SECRET`: any random string, e.g. `openssl rand -hex 32`.
- `DAIMON_MCP__PUBLIC_URL`: `http://localhost:8765/mcp` is fine for local use.
- `POSTGRES_PASSWORD`: a strong, URL-safe value (avoid `@ : / % #`).
- `DAIMON_CRYPTO__KEYS`: a Fernet key, from
  `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
  Paste the key as is. During rotation, list several keys newest first,
  comma-separated (`NEWKEY,OLDKEY`) or as a JSON list (`["NEWKEY","OLDKEY"]`).
  Agent keys, workspace tokens and MCP tokens are encrypted with it. Without
  it every agent key write is refused (see
  [Agent environment encryption](#agent-environment-encryption)). Back it up
  separately from the database.

The first four must be set before the first `docker compose` command:
`docker-compose.yml` interpolates them for every service with fail-fast
`${VAR:?...}` guards. Set `DAIMON_CRYPTO__KEYS` at the same time; after the
stack is up, `docker compose run --rm init "daimon crypto verify"` confirms it
is set and that no agent key is stored in plaintext. `.env` is gitignored, so secrets never get committed.
`.env.example` documents every other setting.
Set `DAIMON_OPS__ALERT_WEBHOOK_URL` to a private Discord channel webhook to
receive short alerts for installs, Stripe top-ups, and Anthropic limits.
Leave it unset to disable operator alerts.

## 2. Create the Discord application

1. Create an application in the
   [Discord Developer Portal](https://discord.com/developers/applications).
2. Under **Bot**, create a bot user and copy its token into `.env` as
   `DAIMON_DISCORD__BOT_TOKEN`.
3. Still under **Bot**, enable the **Message Content Intent**. It is a
   privileged intent; without it the bot cannot read mentions.
4. Under **OAuth2 → URL Generator**, select the `bot` and
   `applications.commands` scopes, then under **Bot Permissions** select at
   least `Send Messages`, `Send Messages in Threads`,
   `Create Public Threads`, `Manage Threads` and `Read Message History`.
5. Open the generated URL and invite the bot to a test server you control.

Setup and routines commands require Discord's `Manage Server` permission.

## 3. Start the stack

```bash
docker compose up --build -d
```

This brings up Postgres, runs migrations and seeds the default agents,
environments and skills (the `init` service does both), then starts the
`mcp`, `discord` and `scheduler` services.

Set `DAIMON_DATABASE__POOL_SIZE`, `DAIMON_DATABASE__MAX_OVERFLOW` and
`DAIMON_DATABASE__POOL_TIMEOUT` per process when sizing Postgres for more
concurrent turns. Each process can open up to `POOL_SIZE + MAX_OVERFLOW`
connections; include every worker and MCP instance in the database limit.

Once it settles, send a message that `@mention`s the bot. It replies in a
new thread. If the bot stays silent, check `docker compose logs discord`; an
unset `DAIMON_DISCORD__BOT_TOKEN` is the usual cause.

### Running a published image instead of building

Every tagged release is published to the GitHub Container Registry as
`ghcr.io/pymc-labs/daimon`, so a host that should not compile anything can run
a release straight from the registry. All six application services run the
same image and differ only in the command Compose gives them, so one `image:`
line per service is the whole change. Put it in `docker-compose.override.yml`,
which Compose reads on top of `docker-compose.yml` automatically:

```yaml
services:
  init:
    image: ghcr.io/pymc-labs/daimon:0.2.0
  mcp:
    image: ghcr.io/pymc-labs/daimon:0.2.0
  discord:
    image: ghcr.io/pymc-labs/daimon:0.2.0
  slack:
    image: ghcr.io/pymc-labs/daimon:0.2.0
  teams:
    image: ghcr.io/pymc-labs/daimon:0.2.0
  scheduler:
    image: ghcr.io/pymc-labs/daimon:0.2.0
```

Then `docker compose pull && docker compose up -d`. Drop the `--build` flag
from step 3 and from the Slack and Teams profile commands: it rebuilds from the working
tree under the same tag and throws the pulled image away. The image ships
only code; `init` still seeds `defaults/` from your checkout, so check out the
matching tag first (`git checkout v0.2.0`). Upgrading is checking out the
next tag, editing the override and running those two commands again.

### Running the processes by hand

Requires [`uv`](https://docs.astral.sh/uv/):

```bash
uv sync --all-extras --all-packages
docker compose up -d postgres
export DAIMON_DATABASE_URL=postgresql+asyncpg://daimon:<your-POSTGRES_PASSWORD>@localhost:5432/daimon
uv run alembic upgrade head
uv run daimon defaults apply
uv run python -m daimon.adapters.discord
```

The `export` is required because the `alembic` CLI reads the shell
environment and does not load `.env`. Slack and Teams start the same way,
with `uv run python -m daimon.adapters.slack` and
`uv run python -m daimon.adapters.teams`.

### Several MCP instances

The MCP endpoint is stateless: it issues no session id and every request
carries its own token, so `mcp` replicas need no sticky sessions behind a load
balancer, and a redeploy does not strand connected clients. The hourly limits
on notebook publishes and bundle uploads are kept in memory, so each replica
counts its own.

## Slack (optional)

Slack needs a publicly reachable `DAIMON_MCP__PUBLIC_URL`. The bot token is
issued by an OAuth install callback served by the `mcp` process, not read
from an env var, and Slack will not redirect to `localhost`.

1. Create the Slack app from
   [`slack-app-manifest.yaml`](slack-app-manifest.yaml) and follow the steps
   in its header comment. It fills in the scopes, slash commands, events and
   Socket Mode toggles.
2. Put the resulting `DAIMON_SLACK__SIGNING_SECRET`, `DAIMON_SLACK__APP_TOKEN`,
   `DAIMON_SLACK__CLIENT_ID` and `DAIMON_SLACK__CLIENT_SECRET` in `.env`, plus
   `DAIMON_CRYPTO__KEYS` (a Fernet key; the adapter stores workspace tokens
   encrypted and refuses to start without one).
3. `docker compose --profile slack up --build -d`
4. Open `https://<your-host>/oauth/slack/install` and install to a workspace.

[`slack.md`](slack.md) covers the trust model for per-user Slack access.
Read it before enabling that feature.

## GitHub App (optional)

Register one GitHub App for this deployment. Set its user authorization callback
to `<root>/oauth/github/callback` and its setup URL to
`<root>/oauth/github/setup`, where `<root>` is the public MCP URL with the
trailing `/mcp` removed. For example, if `DAIMON_MCP__PUBLIC_URL` is
`https://example.com/mcp`, use `https://example.com/oauth/github/callback` and
`https://example.com/oauth/github/setup`. GitHub must be able to reach those
URLs, so a localhost URL only works with a suitable tunnel.

Set `DAIMON_GITHUB_APP__APP_ID`, `APP_SLUG`, `PRIVATE_KEY`, `CLIENT_ID` and
`CLIENT_SECRET` in `.env` using the values from that App. Set
`DAIMON_CRYPTO__KEYS` to encrypt the short-lived user tokens. The connect routes
are mounted only when these values and `DAIMON_MCP__PUBLIC_URL` are present.
The scheduler removes expired connection flows, including their encrypted
tokens. Run it alongside the MCP service.

A server admin can print a seven-day, single-use invitation with
`daimon github connect-link --tenant <workspace-uuid>`. The recipient signs in
to GitHub and confirms the repositories they administer. No repository is
preselected.

## Microsoft Teams (optional)

Teams takes the most setup of the three platforms, because the pieces live in
four Microsoft admin portals:

- **Entra** (the organisation's directory) holds the app's identity.
- **Azure** hosts the bot registration that Teams talks to.
- **The Teams admin center** decides who may install the app.
- **Teams itself** is where you add it.

None of it is hard, but the order matters. Plan on an hour the first time.

Two things work differently from Discord and Slack:

- **Teams pushes messages to you.** Microsoft delivers every message to an
  HTTPS address you give it, so the Teams service needs a public hostname
  with a valid certificate. Discord and Slack dial out for messages instead.
- **One deployment serves one organisation.** The bot answers only people in
  the Microsoft 365 organisation it is registered in, and turns away
  messages from anywhere else, except from people of another organisation
  (external participants and guests) inside a channel kept to its own agents
  (below).

What the bot does once it is running (1:1 chats, channel threads, commands,
files and its limits) is in [`teams.md`](teams.md).

### Before you start

You need:

- **A Microsoft 365 organisation with Teams, and a work account inside it.**
  Do every step below signed in with that account. A personal Microsoft
  account, or an address from another provider, won't work: signing up for
  Azure with one creates a separate, empty directory, and a bot registered
  there can't talk to your Teams.
- **An Azure subscription in that same organisation.** Microsoft 365 doesn't
  include one. Pay-As-You-Go is enough, and the bot itself costs nothing:
  the Azure Bot's F0 tier and its Teams channel are free. Adding a small
  budget alert under Cost Management is cheap insurance.
- **A public HTTPS hostname** for the Teams service, such as
  `teams.example.com`, with a publicly trusted certificate.
- **The right roles**, or someone who has them:

| Step | Who can do it |
| --- | --- |
| Register the app in Entra | Anyone, if your organisation lets users register apps; otherwise the Application Developer role |
| Create the Azure Bot | Contributor (or Owner) on the subscription or a resource group |
| Upload the app to Teams | A Teams administrator, or anyone if custom app uploads are allowed |
| Turn on channel files (optional) | A global administrator (or Privileged Role Administrator) to consent once, then a SharePoint or global admin per team |

### 1. Register the app in Entra

This registration is the identity daimon signs in as.

1. Go to [entra.microsoft.com](https://entra.microsoft.com) → **App
   registrations** → **New registration**.
2. Give it a name (`daimon` is fine) and choose **Accounts in this
   organizational directory only**. Leave the redirect URI empty, then
   **Register**.
3. On the Overview page, copy two values:
    - **Application (client) ID**: this is `DAIMON_TEAMS__CLIENT_ID`.
    - **Directory (tenant) ID**: this is `DAIMON_TEAMS__TENANT_ID`.
4. **Certificates & secrets** → **New client secret**. Copy the **Value**
   (not the Secret ID) straight into your password manager or `.env`: it is
   shown only once. This is `DAIMON_TEAMS__CLIENT_SECRET`. Note its expiry
   date, because the bot stops answering the day it lapses.

You don't need to add any API permissions here. The bot reads channels
through a team-level permission that a team owner grants when they add the
app (more on that in step 7). Only the optional channel files need a Graph
permission.

### 2. Create the Azure Bot and connect it to Teams

The Azure Bot is the registration Teams uses to find your bot.

1. In [portal.azure.com](https://portal.azure.com), create a resource group
   (say, `daimon`) if you don't have one.
2. **Create a resource** → search for **Azure Bot** → **Create**:
    - **Pricing tier:** F0 (Free).
    - **Type of App:** Single Tenant.
    - **Creation type:** Use existing app registration, then paste the
      client ID and tenant ID from step 1.
3. Leave the messaging endpoint for now; you'll set it in step 4, once the
   Teams service is reachable.
4. Open the new bot → **Channels**. Under *Available channels*, click the
   name **Microsoft Teams**. It's a link, not a button. Accept the terms,
   choose **Microsoft Teams Commercial**, and click **Apply** (you may need
   to scroll down). Teams should then appear in the channel list as
   *Healthy*.

If clicking the name does nothing, your account probably has read-only
access to the bot; check **Access control (IAM)** → **View my access**. As a
fallback, open Cloud Shell (the `>_` icon in the portal's top bar) and run:

```bash
az bot msteams create -g <resource-group> -n <bot-name>
```

### 3. Run the Teams service

**Find your admins.** Teams doesn't tell bots who its admins are, so you list
daimon's admins yourself, by their Entra object ID: Entra → **Users** → pick
the person → copy **Object ID**. Admins can create routines, mint
coding-tool tokens, replace shared keys, top up and see everyone's usage.
Anyone can create an agent. You can change the list later.

**Add the values to `.env`:**

```bash
DAIMON_TEAMS__CLIENT_ID=<application (client) ID>
DAIMON_TEAMS__TENANT_ID=<directory (tenant) ID>
DAIMON_TEAMS__CLIENT_SECRET=<client secret value>
DAIMON_TEAMS__ADMIN_USER_IDS=["<object ID>","<another object ID>"]
```

A few things to know:

- **Set the three credentials together.** Once any `DAIMON_TEAMS__` value is
  set, every daimon process expects the full set. A half-filled block stops
  them all from starting, not just Teams.
- **The admin list is a JSON array**, not a comma-separated list.
- **`DAIMON_CRYPTO__KEYS` must be set** (see step 1 of this guide).
- **The `mcp` service reads these same values**, because the agent's Teams
  tools (posting, reading channels, sending files) run there. With Compose,
  sharing `.env` takes care of that.

**Start it:**

```bash
docker compose --profile teams up --build -d
```

The service listens on `127.0.0.1:3978` (`DAIMON_TEAMS__PORT`) and serves
`/api/messages`, `/healthz` and `/readyz`. `docker compose logs teams` should
show `teams.tenant_ready` once it has set up your organisation.

**Put it behind your public hostname.** Microsoft must reach `/api/messages`
over HTTPS. Any reverse proxy or tunnel works. With
[Caddy](https://caddyserver.com), which fetches the certificate for you:

```text
teams.example.com {
	@teams path /api/messages /healthz /readyz /oauth/teams/files/callback
	handle @teams {
		reverse_proxy 127.0.0.1:3978
	}
	handle {
		respond 404
	}
}
```

Only those four paths need to be public. The last one is used only by the
optional channel files. Check it from outside your network:
`curl https://teams.example.com/healthz` should return `{"status":"live"}`.

### 4. Point the bot at daimon

Back in the Azure Bot → **Settings** → **Configuration**, set the
**Messaging endpoint** to your hostname **with `/api/messages` on the end**:

```text
https://teams.example.com/api/messages
```

Forgetting that suffix is the most common reason a bot never answers. While
you're on that page, check that **Microsoft App ID** is your client ID,
**App Tenant ID** is your tenant ID, and **App type** is SingleTenant. Then
**Apply**. Changes can take a few minutes to reach Teams.

### 5. Build the Teams app package

Teams installs apps from a zip file holding three files at its top level:

- `manifest.json`;
- `color.png`, a 192×192 full-colour icon;
- `outline.png`, a 32×32 icon, white on a transparent background.

The manifest template is
[`teams-app-manifest.yaml`](teams-app-manifest.yaml), in manifest version
1.25 so the app can be added to private and shared channels. Fill in your client ID
and hostname and convert it to JSON. From the repository root, with any
Python 3 that has PyYAML:

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

Before you zip it, check these fields in `manifest.json`:

- `termsOfUseUrl` should point at your own terms page, since daimon serves
  none.
- `websiteUrl` and `privacyUrl` should point at pages you serve. The Teams
  host itself answers 404 outside the four bot paths.
- `name` is what people see. If you run more than one deployment, say a test
  one and a production one, give each its own Entra app, Azure Bot and
  package, with a distinct name such as `daimon (test)`.

Then add the two icons. `assets/icon.png` can be resized to 192×192 for
`color.png`; the white-on-transparent `outline.png` you make yourself. Zip
the three files, not the folder that holds them:

```bash
cd teams-app && zip ../daimon-teams.zip manifest.json color.png outline.png
```

### 6. Upload and allow the app

In the [Teams admin center](https://admin.teams.microsoft.com):

1. **Teams apps** → **Manage apps** → **Upload new app**, then choose the zip.
2. Open the uploaded app → **Users and groups** → **Edit availability**, and
   make it available to everyone or to the people who'll use it. (Tenants not
   yet on app centric management: check that its status is **Allowed** and
   that **Permission policies** allow custom apps.)
3. Optional: under **Setup policies**, add the app to *Installed apps* to
   install and pin it for everyone.

A new upload can take anywhere from a few minutes to a few hours to show up
in the Teams client. If custom app uploads are allowed for users, you can
skip the admin center and sideload instead: in Teams, **Apps** → **Manage
your apps** → **Upload an app**.

### 7. Say hello

- **1:1 chat:** in Teams, **Apps** → **Built for your org** → your app →
  **Add**. That opens a chat with the bot and it greets you. Send `help` or
  `setup`.
- **A team:** on the app's page, open the menu next to **Add** → **Add to a
  team**, then pick the team and a channel. A team owner is asked to allow
  the app to read the team's channel messages. Accept, since that's how the
  bot reads the thread it is asked about. The bot then posts a welcome in
  the team. To ask it something, @mention it in a post or a reply. Pick the
  name from the autocomplete list so it turns into a highlighted mention:
  typed text alone doesn't count.
- **A private or shared channel:** adding the app to the team doesn't add it
  there. The channel's owner adds it from the channel itself, in the
  channel's apps settings. A package built from an
  older template, before manifest 1.25, can't be added to these channels:
  rebuild it and upload it again (see Updating the app).

Group chats aren't supported: the package doesn't offer them, and the bot
turns away any that reach it.

### People from another organisation (optional)

To work with a client's people in one channel, use a Teams shared channel
with B2B direct connect (Microsoft's guides: [Collaborate with external
participants in a
channel](https://learn.microsoft.com/en-us/microsoft-365/solutions/collaborate-teams-direct-connect)
and [Shared channels in Microsoft
Teams](https://learn.microsoft.com/en-us/microsoftteams/shared-channels)):

1. **Cross-tenant access, in both organisations** (Entra → **External
   Identities** → **Cross-tenant access settings**): add the other
   organisation. Yours allows B2B direct connect inbound, theirs outbound,
   for the users involved and the Office 365 application. Changes can take
   up to six hours.
2. **Teams policies** (Teams admin center → **Teams** → **Teams policies**):
   in your organisation, "Create shared channels" and "Invite external users
   to shared channels"; in theirs, "Join external shared channels".
3. **Guest access in Teams stays on.** Shared channels with external
   participants don't use guest accounts, but guest access must be enabled
   to invite them. The SharePoint and Microsoft 365 Groups guest settings
   must stay on too (the default).
4. Create the shared channel, set its readers and writers to `own` in daimon
   (the setup panel's Channel settings, `set_channel_rule` or
   `daimon channels rule set`),
   then add the other organisation's people as channel members. Guests,
   including guests converted to members, can't be added to a shared
   channel; someone who also has a guest account in your organisation is
   added as an external participant (in the admin center, search
   `ext:user@domain.com`).

daimon answers them only there, as a member who can't change its setup.
Guests in standard and private channels and 1:1 chats get the same rules;
list the ones who are colleagues with `daimon tenants access-policy
--add-member-guest <object id>`. `DAIMON_TEAMS__RESTRICT_GUESTS=false` treats
every guest as a member; `DAIMON_TEAMS__RESTRICT_EXTERNAL_PARTICIPANTS=false`
does the same for external participants. To tell guests apart, the manifest
asks for the `ChannelMember.Read.Group` permission: an existing install
needs the rebuilt package uploaded again (see Updating the app) and a team
owner to accept it. See [`teams.md`](teams.md).

### Channel files (optional)

Out of the box, the bot reads images in channel messages, and handles files
in 1:1 chats both ways. Files shared in a channel live in the team's
SharePoint site, which the team-level permission doesn't reach. To let the
bot open those files and save its own outputs to the channel's Files tab:

1. Entra → **App registrations** → your app → **API permissions** → **Add a
   permission** → **Microsoft Graph** → **Application permissions** →
   `Sites.Selected` → **Grant admin consent**. This permission reaches only
   the sites you grant one by one, not every site in the organisation.
2. Same app → **Authentication** → **Add a platform** → **Web**, with the
   redirect URI `https://teams.example.com/oauth/teams/files/callback`.
3. Set `DAIMON_TEAMS__PUBLIC_URL=https://teams.example.com` in `.env` (your
   hostname, without `/api/messages`) and run
   `docker compose --profile teams up -d` so the service picks it up.
4. When a daimon admin shares a file in a team daimon can't open yet, the bot
   posts an **Enable files** card. Click it and sign in as a SharePoint or
   global admin. The first sign-in in your organisation must be a global
   admin, who approves this for everyone. daimon then grants itself access
   to that one team's site, and the next message can read the file.

This covers standard channels. Private and shared channels keep their files
in a separate site, which daimon doesn't use. [`teams.md`](teams.md#channel-files-optional)
also shows how to grant a site by hand.

### If the bot doesn't answer

- **Teams won't add the app and asks you to check that it's registered and
  the Teams channel is enabled:** the Azure Bot's Teams channel isn't on (step 2), or the
  manifest's `id` and `botId` don't match the client ID.
- **No answer anywhere, and nothing in `docker compose logs teams`:** the
  messages aren't reaching daimon. Check the messaging endpoint (step 4),
  including the `/api/messages` suffix, and that `/healthz` answers from
  outside your network.
- **1:1 chats work but channel @mentions don't:** if the app was added to the
  team before the messaging endpoint was right, the team never linked up.
  Remove the app from the team (team → ⋯ → **Manage team** → **Apps**) and
  add it again. Check, too, that the mention was picked from autocomplete.
- **Every daimon process fails at startup after adding Teams:** a
  `DAIMON_TEAMS__` value is missing, or `DAIMON_TEAMS__ADMIN_USER_IDS` isn't
  a JSON array of object IDs.
- **`teams.tenant_reconcile_failed` in the logs:** daimon couldn't finish
  setting up your organisation, and it refuses turns until it does. The log
  line says why.
- **It stopped answering after months of working:** the client secret has
  probably expired. Create a new one (step 1), update `.env` and run
  `docker compose --profile teams up -d`, which also recreates `mcp`.

### Updating the app

Changes to the manifest only reach Teams in a new package. Bump `version` in
`manifest.json`, zip it again and upload it over the existing app in the
Teams admin center (the app → **Upload file**). Teams that already have the
app pick up the update, and a team owner may be asked to accept new
permissions.

## Claude Code login mounts

Coding-agent clients such as Claude Code connect through the plugin in
[`plugin/`](https://github.com/pymc-labs/daimon/blob/main/plugin/README.md) instead of a per-agent token. It logs in via
Slack or Discord OAuth and reaches every daimon install the logged-in person
belongs to. There is no Teams login yet.

Each platform's mount needs its own OAuth app, plus `DAIMON_HUB__*`,
`DAIMON_CRYPTO__KEYS` (login state is encrypted at rest) and
`DAIMON_MCP__PUBLIC_URL` (the mounts derive their public base URL from it).
Register these redirect URIs on the OAuth apps, where `{origin}` is
`DAIMON_MCP__PUBLIC_URL` without the trailing `/mcp`:

- Slack: `{origin}/slack/auth/callback`
- Discord: `{origin}/discord/auth/callback`

The Discord app requests the `identify` and `guilds` scopes. The Slack app
requests user scopes (`users:read`, `channels:history`, `groups:history`,
`channels:read`, `groups:read`, `im:history`, `mpim:history`, `im:read`,
`mpim:read`, `search:read`) so a daimon reads Slack as the person asking and
never sees a channel they cannot.

A login reaches only workspaces where daimon is installed and ready, checked
on every call. Membership is re-read when the login token is issued or
refreshed: a Slack token stops working the moment its user leaves the
workspace, while someone removed from a Discord server keeps that server's
daimons until their Discord token expires.
`DAIMON_HUB__ALLOWED_CLIENT_REDIRECT_URIS` limits which clients may complete
a login; the default covers coding agents on loopback and claude.ai.

## Chart delivery and artifact storage

Hosted MCP clients receive bounded chart images directly from the Anthropic
Files API. This embed-only path is enabled by default and needs no bucket.

Clients that orchestrate `start_turn`, `get_my_session` and `list_events`
themselves can call `deliver_turn_charts(handle)` after `get_my_session`
reports `idle` or `terminated` to receive the same chart payload. Calls made
while a turn is running or rescheduling are refused.

To add short-lived presigned chart links, configure
`DAIMON_ARTIFACTS__ENDPOINT_URL`, `DAIMON_ARTIFACTS__BUCKET`,
`DAIMON_ARTIFACTS__ACCESS_KEY_ID` and `DAIMON_ARTIFACTS__SECRET_ACCESS_KEY`.
When URL delivery is configured, `deliver_turn_charts` writes the chart to
the private artifact store.

- The storage boundary uses the vendor-neutral S3 API with SigV4 and
  virtual-hosted-style addressing. Confirm that contract with your provider.
  Path-style-only endpoints, including default MinIO setups, are not
  supported.
- Objects remain private. daimon never applies a public-read ACL.
- Presigned URL expiry does not delete stored objects. Configure a bucket
  lifecycle rule for the retention period your deployment requires.

See `.env.example` for the optional region, URL lifetime and image-embedding
controls.

## Connecting Notion, Linear and other MCP servers

Ask the agent to connect a server and it posts a card only you can open.

- A server that takes a bearer token (Linear, GitHub) gets a private token
  form. The token is checked against the server before it is stored and is
  shared by everyone who talks to that agent.
- A server that signs people in through a browser (Notion, Slack, Atlassian)
  gets a sign-in link. daimon registers itself as an OAuth client, you
  approve in the browser, and the grant lands in your own vault, refreshed
  by Anthropic. Each person connects their own account.

The sign-in routes live at `{origin}/oauth/mcp/start` and
`{origin}/oauth/mcp/callback`, so `DAIMON_MCP__PUBLIC_URL` must be reachable
from a browser and `DAIMON_CRYPTO__KEYS` must be set. If one connection
fails, the agent still answers and names the server it could not use under
the reply. Ask it to disconnect the server or connect it again.

## Backup and disaster recovery

Use managed Postgres with point-in-time recovery for production. Set a recovery
point objective (maximum acceptable data loss) and a recovery time objective
(maximum outage), then schedule and rehearse backups to meet them. A dump on the
same disk as the database does not protect against disk loss. Keep encrypted,
off-host copies with retention and access controls, and retain the application
release/commit and migration revision alongside each recovery set.

### Backup contract

| State class | What to preserve | Recovery contract |
| --- | --- | --- |
| Postgres | Whole database: tenants, encrypted credentials, mappings, routines, usage, balances, and Alembic revision | Consistent logical snapshot via the optional service below, or provider PITR. Restore into a fresh DB with the same major Postgres version and matching application release. Roles/ownership/grants are intentionally excluded; the restore user owns the objects. |
| Encryption and configuration | All active **and historical** `DAIMON_CRYPTO__KEYS`, signing secrets, OAuth/API credentials, `.env`, Compose overrides, defaults and pinned code | Keep in a separate encrypted secret-manager backup. Losing a decryption key makes its stored ciphertext unrecoverable. Restore keys before starting services; do not generate replacement keys for existing ciphertext. |
| Report and notebook hosts | Their configured persistent data directories/volumes, including published files, source bundles and metadata | Stop writers, snapshot/copy the entire directories with permissions, then restore to the same mount paths. The main Compose file does not mount these optional hosts: inventory their actual deployments. |
| Media and artifact stores | Media file-store data and any configured S3-compatible artifact bucket | Snapshot local media directories; enable bucket versioning/backup independently of URL expiry and lifecycle deletion. Restore keys/paths unchanged; expired presigned links must be reissued. |
| Managed Agents objects | Current agents/environments, every downloadable custom skill version, current memory-store contents | `daimon backup platform-export` creates a workspace-wide operator archive. Existing DB mappings remain valid only in the original intact MA workspace. Loss of that workspace requires manual recreation and ID remapping. |
| MA transcripts and session disks | Platform-retained session events, files and ephemeral sandboxes | **Not covered** by the object export or database dump. Do not promise recovery of ongoing sessions. Retain important deliverables in backed-up stores; start fresh sessions after platform loss. |

Quiesce adapters, scheduler, webhook ingestion and optional file hosts before
capturing a coordinated recovery set. A Postgres dump is internally consistent,
but it cannot make file stores and MA atomic with the database. Stop active MA
turns and edits too. Record capture times and any changes allowed during capture.
The service below only backs up Postgres, not the other rows of this table.

### Create backups

With the normal Compose environment configured:

```bash
mkdir -p backups
chmod 700 backups
docker compose --profile backup run --rm backup backup /backups/2026-09-28T220000Z
uv run daimon backup platform-export backups/platform-2026-09-28T220000Z.zip
```

Use a unique timestamp each time; existing destinations are never overwritten.
`DAIMON_BACKUP_DIR` can select a different host directory. Schedule the one-shot
Compose command with your host scheduler; no backup daemon runs by default.
A completed database directory contains `database.dump` and `SHA256SUMS`.
A failed backup may leave an incomplete directory: never treat it as usable
without its checksum file and a successful restore drill. Encrypt and copy the
recovery set off-host; the checksum detects corruption, not malicious tampering.

The platform command only reads the dedicated MA workspace configured by
`DAIMON_ANTHROPIC__API_KEY`. It does not run model turns. Its private (0600) ZIP
contains JSON definitions with original IDs and metadata, nested skill ZIPs,
full memory contents, and a versioned manifest with per-entry SHA-256 hashes.
It includes all tenants in that workspace and can contain private data and
secret-bearing definitions: restrict access like a database dump. Upstream
failures or a full, potentially truncated skills page abort the export without
publishing a partial ZIP. Remote names and
memory paths remain data, never filesystem paths.

This first version is **not replayable by `defaults apply`**. That importer owns
deployment defaults, not arbitrary tenant objects or memory contents. Retain
original `defaults/` and skill sources for `daimon defaults apply`; use exported
JSON/skill ZIPs for manual tenant-object reconstruction, restore memory paths
and contents through MA, then reconcile database IDs before enabling traffic.
Vault secrets, historic agent/environment/memory versions, and transcripts are
excluded. This archive is evidence and recovery material, not a full MA clone.

### Restore a database

Keep adapters and scheduler stopped. Provision an **empty** database first and
restore with the Postgres 18 client image (or matching local client tools).
Set `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD` and `PGDATABASE` through your secret
manager/environment, never put passwords in command arguments. With Compose,
for a new database named `daimon_recovery` on its Postgres service:

```bash
docker compose exec postgres sh -c 'createdb -U "$POSTGRES_USER" daimon_recovery'
docker compose --profile backup run --rm \
  -e PGDATABASE=daimon_recovery -e DAIMON_RESTORE_DATABASE=daimon_recovery \
  backup restore /backups/2026-09-28T220000Z
```

For local client tools the equivalent command is:

```bash
DAIMON_RESTORE_DATABASE="$PGDATABASE" sh scripts/backup/postgres.sh restore backups/2026-09-28T220000Z
```

The command validates the checksum, requires explicit target-name confirmation,
refuses a database containing relations, and runs `pg_restore` in one transaction
with errors fatal. It never drops existing data. Restore only trusted archives:
Postgres dumps contain executable SQL. Use a dedicated empty target with no
concurrent writers. On failure, investigate before retrying; do not bypass the
empty-database guard to overwrite a running deployment.

Restore file stores and the original keys, point application configuration at
the recovered database, and start the matching application release first.
Inspect `alembic_version`, row counts and representative tenant/credential,
routine, usage and published-file records. Verify decryption and platform IDs
before enabling traffic or scheduling. Apply upgrades only after this check.
If the original MA workspace survives, retain its mappings; if it was lost, do
not resume routines until manual object reconstruction and ID remapping finish.

### Local restore drill

```bash
sh scripts/backup/drill.sh
```

This runs the actual backup/restore commands against a disposable,
network-isolated Postgres container and removes only that container on exit.
It verifies rows, a migration marker and an identity sequence, plus rejection of
nonempty targets, missing confirmation and corrupt dumps. It uses no deployment
credentials. Also rehearse your real recovery set in an isolated environment:
this small drill does not establish your production recovery time or prove that
external stores, keys and MA mappings are complete.

### Agent environment encryption

Agent environment values in `agent_files.content` are encrypted with
`DAIMON_CRYPTO__KEYS`, using the same MultiFernet key ring as other
credentials. Names and attribution remain readable. A deployment without keys
still starts and serves existing values, logs `agent_env.encryption_keys_missing`
at error level when the session factory starts, and refuses every agent key
write (the credential form and `request_agent_key` say so; nothing is saved).
For local development only, `DAIMON_CRYPTO__ALLOW_PLAINTEXT=true` restores
plaintext storage and logs `agent_env.encryption_disabled` instead.

`daimon crypto verify` exits non-zero when keys are missing, any agent key is
stored in plaintext, or an encrypted key can't be decrypted with the current
keys (a key retired too early), listing the count per tenant (never names or
values).
`daimon crypto encrypt-plaintext` encrypts every plaintext row in place with the
first key, in one transaction, leaving timestamps and attribution unchanged.
Run it after enabling keys on a deployment that stored keys without them, and
only once **every** process (MCP, Discord, Slack, Teams, scheduler) has restarted with
the keys: a process still running without keys can't read the rows it encrypts,
and turns for those agents fail. Then run `verify` again. Add `verify` to your
onboarding checklist.

Stop old application readers and writers before upgrading through
`0028_agent_env_encryption`, since old readers cannot decode encrypted values.
With keys configured, the migration encrypts existing rows in one transaction;
without keys it adds storage metadata but leaves every value unchanged. The
migration uses a five-second lock timeout, so a
busy database fails the migration instead of waiting indefinitely. Retry during
a maintenance window. It requires an online database connection.

The separate `agent_files.encoding` column defaults to `plain` for legacy rows
and keyless writes. Encrypted rows are tagged `fernet_v1`; each write updates the
value and tag atomically. No text prefix is reserved: even literal values starting
with `enc:v1:` or resembling valid Fernet tokens round-trip unchanged. Store APIs
always accept plaintext user values. Migration retries encrypt only `plain` rows
and preserve existing `fernet_v1` ciphertext without double encryption.

A database trigger treats inserts and updates from old writers as `plain`, even
when they replace an encrypted row. New writers and the migration set the local
transaction marker `daimon.agent_env_writer = 'v1'` while writing explicit encoding
metadata, then clear it. This keeps old upsert/CAS writes readable by new code and
allows rollback during a mixed-writer window; it does not make old readers able
to decrypt new ciphertext. Stop old readers before keyed migration as described
above. Downgrade removes the trigger and its function after decrypting rows.
New code reads legacy `plain` rows, logs a warning containing only row identifiers
when encryption is enabled, and encrypts them on the next write. Wrong keys or
corrupt rows tagged `fernet_v1` fail closed. Wrong-key and corrupt-ciphertext errors
identify the tenant, agent and key name and point at `DAIMON_CRYPTO__KEYS`, never
the value.
The migration logs `agent_env.migration_keyless_noop` when keys are absent; keyed
rewrites log their action and row count. Do not remove keys while
encrypted values remain. When enabling keys after a keyless migration, rewrite
existing values through the application or re-run this migration's data upgrade
under maintenance, or run `daimon crypto encrypt-plaintext`; setting keys alone
does not rewrite existing rows.

For rollback, stop application readers and writers and run the migration's
Alembic downgrade with the original keys available before starting old code.
When this revision is the head, use `alembic downgrade -1`. The downgrade restores
plaintext in the same transaction and refuses to decrypt encrypted rows without
valid keys. It removes the encoding column only after successful decryption;
keyless plaintext-only downgrades leave the values unchanged.

To rotate keys, prepend a new key and retain older keys for reads. Rewriting a
decrypted environment value encrypts it with the first key. Do not retire old
keys until all stored credentials have been re-encrypted. Old backups, WAL, and
dead tuples can still contain plaintext. Normal VACUUM makes dead tuple space
reusable; it is not a secure erasure guarantee. Apply secret-retention controls
to backups and storage. Application encryption protects database-only access;
access to both the database and the key ring permits decryption.
