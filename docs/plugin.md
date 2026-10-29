# Claude Code plugin

Let people ask the daimons in your Slack workspaces and Discord servers a
question from inside Claude Code. Read this before you install the plugin: it
covers what the server side needs. Teams has no login mount, so the plugin
cannot reach daimons on Teams.

## What the plugin is

The plugin declares two HTTP MCP servers, `daimon-slack` and `daimon-discord`,
and carries no credentials. It adds one skill, `daimon-context`, which routes a
question about team context to the right daimons and merges their answers, and
one command, `/daimon:daimon-status`, which lists what is connected. They use
the hub tools in [mcp-tools](mcp-tools.md#hub-login-mounts-hub), a surface
separate from the main MCP tool set. Install steps and login are in the
[plugin README](https://github.com/pymc-labs/daimon/blob/main/plugin/README.md).

## What a login gets you

A login proves one platform identity and its workspaces. Daimon intersects
those with the workspaces where it is installed and ready, on every call. It
then finds or creates your account in each, as in chat. A turn started from
Claude Code is permission-checked and billed as you in that workspace. Slack
reads run with your own Slack visibility.

If you see no daimons, the plugin is rarely the cause. You may not have
authenticated that server with `/mcp`, Daimon may not be installed in any of
your workspaces, or the deployment may have no mount for that platform.

## Before you install

- **Point it at your deployment.** It defaults to the hosted one. Set
  `DAIMON_MCP_URL` to your origin, scheme and host only.
- **Log in per server.** Installing authenticates nothing. Run `/mcp` and
  authenticate each platform you want.
- **Expect normal billing checks.** A turn started through the hub runs the same
  balance and cap checks as any MCP turn, so a workspace out of credit refuses it
  up front. Its cost is debited when a later read or the scheduler's usage
  sweep settles the session, as for any MCP turn. See [billing](billing.md).
- **Install from a checkout.** Run `claude --plugin-dir ./plugin`. The plugin is
  not published to a marketplace.

## Set up the login mounts

The plugin talks to `<origin>/slack/mcp` and `<origin>/discord/mcp`. Each is a
URL path on the MCP server with its own OAuth proxy, so a Slack login token is
never checked by the Discord proxy. A platform's mount appears only when you set
that platform's client ID and secret in the
[`DAIMON_HUB__*` settings](configuration.md#hub). You can offer one platform,
both or neither.

Each mount needs its own OAuth app, plus:

- `DAIMON_CRYPTO__KEYS`, because login state is encrypted at rest.
- [`DAIMON_MCP__PUBLIC_URL`](configuration.md#daimon_mcp__public_url) or
  [`DAIMON_HUB__PUBLIC_URL`](configuration.md#daimon_hub__public_url), the origin
  the mounts are served from. The server refuses to boot if a mount is
  configured without them.

Set `DAIMON_HUB__PUBLIC_URL` when coding agents reach the mounts on a different
hostname from `DAIMON_MCP__PUBLIC_URL`. Changing `DAIMON_MCP__PUBLIC_URL`
instead re-points every agent's stored MCP credential.

Register these redirect URIs on the OAuth apps, where `{origin}` is
`DAIMON_HUB__PUBLIC_URL`, or `DAIMON_MCP__PUBLIC_URL` without the trailing
`/mcp` when that is unset:

- Slack: `{origin}/slack/auth/callback`
- Discord: `{origin}/discord/auth/callback`

The Discord app requests the `identify` and `guilds` scopes. The Slack app
requests user scopes (`users:read`, `channels:history`, `groups:history`,
`channels:read`, `groups:read`, `im:history`, `mpim:history`, `im:read`,
`mpim:read`, `search:read`), so Daimon reads Slack as the person asking and
never sees a channel they cannot.

A login reaches only workspaces where Daimon is installed and ready, checked on
every call. Membership is re-read when a login token is issued or refreshed. A
Slack token stops working as soon as its user leaves the workspace. Someone
removed from a Discord server keeps that server's daimons until their Discord
token expires.
[`DAIMON_HUB__ALLOWED_CLIENT_REDIRECT_URIS`](configuration.md#daimon_hub__allowed_client_redirect_uris)
limits which clients may complete a login. The default covers coding agents on
loopback and claude.ai.

Run the scheduler alongside the mounts. It prunes expired login state, and
without it the table grows by a row per login attempt.
