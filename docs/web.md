# Web app

The web app lets people chat with Daimon's agents in a browser. Admins also
manage members, credit and settings there. Read this if you run the
deployment.

## Before you start

You need:

- **A running deployment.** Follow [Self-hosting](self-hosting.md) first.
- **A public HTTPS hostname** such as `daimon.example.com`, served by a
  reverse proxy you run.
- **Encryption keys** in
  [`DAIMON_CRYPTO__KEYS`](configuration.md#daimon_crypto__keys). Sign-in tokens
  and saved keys are stored encrypted.
- **At least one sign-in method:** your own OpenID Connect provider, or the
  Discord, Slack or Microsoft login apps the deployment already has.

## Turn it on

1. Set [`DAIMON_WEB__PUBLIC_URL`](configuration.md#daimon_web__public_url) to
   the address people open, with no path.
2. Set up a sign-in method, as described below.
3. Start the web process. Run only one: it keeps running replies in memory, so
   stop the old process before you start a new one. A second process refuses to
   start.

   ```bash
   docker compose --profile web up -d
   ```

4. Point your proxy at `127.0.0.1:8780`. Change the port with
   [`DAIMON_WEB__PORT`](configuration.md#daimon_web__port). Let request bodies
   reach 25 MB, so file uploads get through.
5. Check that `https://daimon.example.com/healthz` answers.

[`DAIMON_WEB__MODULES`](configuration.md#daimon_web__modules) turns sections
on or off for the whole deployment. Workspace admins can switch them off again
for their own workspace under **Admin** > **Settings**.

## Set up sign-in

[`DAIMON_WEB__PROVIDERS`](configuration.md#daimon_web__providers) lists the
methods offered. A method shows on the sign-in page only when its settings
are complete too. Each method returns to a redirect URI you register with it:

| Method | Redirect URI |
| --- | --- |
| OpenID Connect | `https://daimon.example.com/auth/oidc/callback` |
| Discord | `https://daimon.example.com/auth/discord/callback` |
| Slack | `https://daimon.example.com/auth/slack/callback` |
| Microsoft | `https://daimon.example.com/auth/microsoft/callback` |

### OpenID Connect

Use this when your team signs in through its own identity provider.

1. Register a confidential client with the provider. Add the redirect URI
   above.
2. Find the workspace people should join:

   ```bash
   docker compose run --rm init "daimon tenants list --json"
   ```

3. Set the issuer, client and workspace:
   [`DAIMON_WEB__OIDC__ISSUER`](configuration.md#daimon_web__oidc__issuer),
   [`DAIMON_WEB__OIDC__CLIENT_ID`](configuration.md#daimon_web__oidc__client_id),
   [`DAIMON_WEB__OIDC__CLIENT_SECRET`](configuration.md#daimon_web__oidc__client_secret)
   and
   [`DAIMON_WEB__OIDC__TENANT_ID`](configuration.md#daimon_web__oidc__tenant_id).
4. Optionally limit who may join with
   [`DAIMON_WEB__OIDC__ALLOWED_DOMAINS`](configuration.md#daimon_web__oidc__allowed_domains).

Everyone who signs in joins that workspace as a member. Their groups come from
[`DAIMON_WEB__OIDC__GROUPS_CLAIM`](configuration.md#daimon_web__oidc__groups_claim),
and spend allowances can name a group. Add `offline_access` to
[`DAIMON_WEB__OIDC__SCOPES`](configuration.md#daimon_web__oidc__scopes) when
embedded apps need the person's token for longer than one access token lasts.

A provider that verifies people's chat accounts can list them in a
`chat_accounts` claim. With
[`DAIMON_WEB__OIDC__TRUST_CHAT_ACCOUNTS`](configuration.md#daimon_web__oidc__trust_chat_accounts)
on, a person also reaches each chat workspace those accounts are in, as that
account, the same as signing in with Discord, Slack or Microsoft. Where the
workspace's member list is loaded, the account must be on it; for Teams, and for
a workspace whose list has not loaded yet, the claim alone is the proof. A chat
account that already signs in with another account of the same provider is not
linked again: the sign-in skips that workspace and the audit log records why.

### Discord, Slack and Microsoft

These reuse the deployment's existing login apps, so there is nothing new to
create. Add the redirect URI to each app you offer:

- Discord: the app in
  [`DAIMON_HUB__DISCORD_CLIENT_ID`](configuration.md#daimon_hub__discord_client_id).
- Slack: the app in
  [`DAIMON_HUB__SLACK_CLIENT_ID`](configuration.md#daimon_hub__slack_client_id).
- Microsoft: the app registration in
  [`DAIMON_MICROSOFT_ACCOUNTS__CLIENT_ID`](configuration.md#daimon_microsoft_accounts__client_id).

People who sign in this way reach every workspace with Daimon installed where
their chat account is a member. Their spend and memory are the same as in
chat. Someone who leaves a Discord server or Slack workspace loses it in the
web app at once. Discord needs the Server Members intent and Slack the
`team_join` and `user_change` events for this; until a list loads, the sign-in
itself decides. Slack lists also reload after a reconnect and every six hours,
since Slack does not resend events missed while disconnected.

## Become the owner

A new workspace has nobody who can grant roles in the web app. Make yourself
its owner with a one-time link:

1. Sign in to the web app once.
2. Create the link. Add `--workspace <tenant-uuid>` for a workspace other than
   the OpenID Connect one:

   ```bash
   docker compose run --rm web owner-link
   ```

3. Open the link in the same browser within a day. Change the time with
   `--days`.

From then on, invite people and grant roles under **Admin** > **People**.

## Embed an app

An embedded app is a separately hosted site shown inside the web app in a
frame. Daimon knows nothing of any particular app: an app is configuration.
List apps in [`DAIMON_WEB__APPS`](configuration.md#daimon_web__apps) as a JSON
list. This one is a notes app at `notes.example.com` that shows its own view of
each agent:

```bash
DAIMON_WEB__APPS='[{"key": "notes", "name": "Notes", "icon": "plug",
  "origin": "https://notes.example.com",
  "connections_path": "/connections",
  "nav": [{"label": "Notes", "path": "/notes", "icon": "database"}],
  "panels": {"agent": {"embed": "/embed/agents/{agent_id}",
                       "page": "/agents/{agent_id}"}},
  "oidc_issuer": "https://id.example.com"}]'
```

| Field | What it does |
| --- | --- |
| `key` | Short id used in web app URLs, unique among the apps. |
| `name`, `icon` | What the web app shows. An icon is `database`, `plug`, `key-round` or `link` (another Lucide name shows `database`), or a `data:image/svg+xml;base64,` or `data:image/png;base64,` URI of at most 16 KB. |
| `origin` | Where the app is served, with no path. Only these origins may load in a frame. |
| `nav` | Sidebar entries, each a `label`, `path` and `icon`. |
| `connections_path` | Page where people manage the connections the app serves to agents. |
| `panels.agent` | The app's view of one agent: `embed`, the path shown in a drawer, and `page`, the same view as a full page. Both are paths starting with `/` that contain `{agent_id}`. |
| `oidc_issuer` | Issuer whose access tokens the app accepts. Set it only for an app you trust with the person's token. |
| `identity_server` | A [`DAIMON_MCP_IDENTITY__SERVERS`](configuration.md#daimon_mcp_identity__servers) entry. People signed in with a chat account then get a token by token exchange at `oidc_issuer`, which must be the web sign-in provider. |

The sign-in button's icon, `DAIMON_WEB__OIDC__ICON`, takes the same forms.

The app must allow framing by your public URL, in its own
`frame-ancestors` policy.

The provider must accept the exchange: an RFC 8693 request from the web client
whose `subject_token` is a JWT signed with
[`DAIMON_MCP_IDENTITY__SIGNING_KEY`](configuration.md#daimon_mcp_identity__signing_key).
Its `iss` is the entry's `issuer` and `aud` the `oidc_issuer`. `sub` is the chat
account and `ws` the workspace, each a platform and id such as
`discord:123`. A refused exchange is tried again after five minutes.

### Agent panels

`{agent_id}` is the agent's id at MCP servers: the `agent` claim of Daimon's
identity assertion, so the app can match it to what it receives from an agent.
The web app fills it in, URL-encoded, and sends each agent's paths with the
agent. An app appears on an agent's canvas when the agent has an MCP server at
the app's `origin` and the viewer may see the agent's servers. Its node opens the `embed` path in a drawer, with a link to
`page` in the web app.

### What the app serves

The web app opens app pages with `?embed=1` added, and talks to them with
`postMessage`. Each message carries `embed: 1` and a `type`, and only the
app's exact origin is answered.

- **Token.** The frame sends `{embed: 1, type: "token"}`. The web app answers
  `{embed: 1, type: "token", token}`. `token` is the person's own access token
  from the `oidc_issuer` provider, refreshed when nearly spent. It is valid
  wherever Daimon's client tokens are accepted. With `identity_server`, someone
  signed in another way gets one by token exchange instead. Check its signature
  against the issuer, and check `aud` and `azp` are what you expect. A person
  with no token does not see the app.
- **From the frame.** `navigate` with `path` records where a page frame is, so
  reloads and links return there. `title` with `title` sets the browser tab.
  `open-nav` opens the sidebar on small screens. `keys` with `key` and `mod`
  hands shortcuts to the web app: `k` with `mod` opens search, `?` lists
  shortcuts, and `g` plus a letter jumps to a section. Any other `type` is
  ignored.
- **To the frame.** `navigate` with `path` moves a page frame when someone
  clicks the sidebar. `theme` with `light` or `dark` is sent on load and when
  the theme changes. Both page and drawer frames get `theme`.
- **Drawers.** A drawer opens `<origin><embed>?embed=1`. It answers `token` and
  receives `theme`; the web app ignores its other messages.
- **Status.** **Admin** > **Settings** shows each app as connected when
  `<origin>/embed/manifest` answers with success within 3 seconds. The request
  carries the admin's token when there is one, and the answer is reused for 30
  seconds.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| A sign-in button is missing | Add the method to `DAIMON_WEB__PROVIDERS` and complete its settings. |
| The provider reports a redirect URI mismatch | Register the exact URI from the table above. |
| Sign-in works but writes fail with "Cross-site request refused." | `DAIMON_WEB__PUBLIC_URL` must match the address in the browser exactly. |
| An app shows Error in **Admin** > **Settings** | `<origin>/embed/manifest` did not answer with success within 3 seconds. |
| An app is missing for some people | They have no token for it: they signed in another way, or their token lapsed. Add `offline_access` to the scopes, or set `identity_server`. |
