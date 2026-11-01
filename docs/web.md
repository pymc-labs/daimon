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
- **Discord sign-in:** the Discord login app the deployment already has.

## Turn it on

1. Set [`DAIMON_WEB__PUBLIC_URL`](configuration.md#daimon_web__public_url) to
   the address people open, with no path.
2. Set up sign-in, as described below.
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

People sign in with Discord. The web app reuses the login app in
[`DAIMON_HUB__DISCORD_CLIENT_ID`](configuration.md#daimon_hub__discord_client_id),
so there is nothing new to create. Add this redirect URI to it:
`https://daimon.example.com/auth/discord/callback`.

People reach every workspace with Daimon installed where their Discord account
is a member. Their spend and memory are the same as in chat. Someone who leaves
a Discord server loses it in the web app at once. This needs the bot's Server
Members intent. Until the member list loads, or while the intent is off, the
sign-in itself decides.

## Become the owner

A new workspace has nobody who can grant roles in the web app. Make yourself
its owner with a one-time link:

1. Sign in to the web app once.
2. Find the workspace id, then create the link:

   ```bash
   docker compose run --rm init "daimon tenants list --json"
   docker compose run --rm web owner-link --workspace <tenant-uuid>
   ```

3. Open the link in the same browser within a day. Change the time with
   `--days`.

From then on, invite people and grant roles under **Admin** > **People**.

## Connect agents to a data server

A connection node lets agent editors link an agent to a server that keeps its
own agents and access rules. The server must already be a signed-identity
server in
[`DAIMON_MCP_IDENTITY__SERVERS`](configuration.md#daimon_mcp_identity__servers),
with [`DAIMON_MCP_IDENTITY__SIGNING_KEY`](configuration.md#daimon_mcp_identity__signing_key)
set. Name it in the
[`DAIMON_WEB__NODES__DATA_SERVER__*`](configuration.md#daimon_web__nodes__data_server__identity_server)
settings:

```bash
DAIMON_WEB__NODES__DATA_SERVER__IDENTITY_SERVER=data
DAIMON_WEB__NODES__DATA_SERVER__CLIENT_ID=daimon-web
DAIMON_WEB__NODES__DATA_SERVER__CLIENT_SECRET=...
```

Connecting an agent links its agent on the server and attaches the server's
MCP relay to it. Linking fails when another person manages that agent. Disconnecting removes the relay and keeps the server's agent.
Only people who may edit the agent connect or disconnect it.

The web app acts on the server as the signed-in person. It gets their token by
RFC 8693 exchange at the server's `/oauth/token`, as the OAuth client above.
The subject token is a JWT signed with the identity key: `iss` is the entry's
`issuer`, `aud` its `audience`, `sub` the Discord account and `ws` the
workspace, such as `discord:123`. Only workspaces the entry serves get a token.
A refused exchange is tried again after five minutes.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| The sign-in button is missing | Set `DAIMON_HUB__DISCORD_CLIENT_ID` and `DAIMON_HUB__DISCORD_CLIENT_SECRET`. |
| Discord reports a redirect URI mismatch | Register the exact URI above. |
| Sign-in works but writes fail with "Cross-site request refused." | `DAIMON_WEB__PUBLIC_URL` must match the address in the browser exactly. |
| A data server says it didn't accept your sign-in | Check the node's client and that the server trusts the identity key. Retries wait five minutes. |
