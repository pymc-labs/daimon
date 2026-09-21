# Claude Code plugin

`plugin/` is a Claude Code plugin that lets you ask the daimons in your Slack
workspaces and Discord servers a question from inside Claude Code.
[`plugin/README.md`](../plugin/README.md) is the user-facing doc: what the
plugin ships, how to install it, and how to log in. This page is the part you
want before you install — what has to be true on the server side for any of
it to work.

## What it is

The plugin declares two HTTP MCP servers, `daimon-slack` and `daimon-discord`,
and carries no credentials of its own. It adds one skill, `daimon-context`,
which routes a question about team context to the right daimons and merges
their answers, and one command, `/daimon:daimon-status`, which lists what is
connected without asking anything. Both are built from the eight tools
registered by
`packages/adapters/mcp/daimon/adapters/mcp/tools/hub.py`, catalogued in
[mcp-tools.md](mcp-tools.md#hub-login-mounts-hub) — a surface separate from
the main MCP tool set.

## The hub login mounts

Those two URLs only exist if the deployment you point at has the hub login
mounts configured, and "mount" here means a URL path, not a file or a volume.
`packages/adapters/mcp/daimon/adapters/mcp/hub/app.py` builds one `FastMCP`
sub-app per platform and mounts it on the MCP server's ASGI app at `/slack`
and `/discord`, each with **its own OAuth proxy**, so a Slack login token is
never evaluated by the Discord proxy. The plugin then talks to
`<origin>/slack/mcp` and `<origin>/discord/mcp`.

A platform's mount appears only when both that platform's client id and secret
are set, so a deployment can offer one, both or neither, and one that sets
neither behaves exactly as it did before the mounts existed. Beyond the
`DAIMON_HUB__*` pair, the mounts need `DAIMON_CRYPTO__KEYS` (login state is
encrypted at rest) and `DAIMON_MCP__PUBLIC_URL` (they derive their public base
URL from it); the server refuses to boot if a mount is configured without
them. The operator side — OAuth apps, redirect URIs, the scopes each platform
asks for — is in
[self-hosting.md](self-hosting.md#claude-code-login-mounts), and the settings
themselves are in [configuration.md](configuration.md#hub).

## What a login gets you

Logging in proves one platform identity and the workspaces it belongs to.
`packages/core/daimon/core/hub_identity.py` intersects that set with the
tenants where daimon is actually installed and ready, and looks up or
provisions your account in each one exactly as the chat adapters do on first
contact — so a turn you start from Claude Code is permission-checked and
billed as you, in that workspace, not as some shared service identity. The
intersection is recomputed on every call, and Slack reads run with your own
Slack visibility.

That also means the answer to "why do I see no daimons" is usually not the
plugin. Either you have not authenticated that server with `/mcp` yet, or
daimon is not installed in any workspace you belong to, or the deployment has
no mount for that platform.

## Before you install

- **Point it somewhere.** It defaults to the hosted deployment. For your own,
  set `DAIMON_MCP_URL` to the origin — scheme and host only, since the plugin
  appends the platform paths itself.
- **Log in per server.** Installing the plugin authenticates nothing; you run
  `/mcp` and authenticate each platform you want.
- **Know the billing behaviour.** A turn started through the hub runs the same
  balance and cap admission checks as any other MCP turn, so a workspace out
  of credit refuses it up front. As with those tools today, it does not record
  a metered debit when it completes. See [billing.md](billing.md).
- **Installing from a marketplace is not available yet.** Until the plugin is
  published, the working path is `claude --plugin-dir ./plugin` from a
  checkout, as [`plugin/README.md`](../plugin/README.md) describes.

One operational note for self-hosters: the hub keeps short-lived login state
in a table that is pruned by a sweep on the scheduler's tick
(`packages/core/daimon/core/hub_oauth_kv_sweep.py`). The store honours
expiry on read but never deletes, so a deployment that runs the mounts
without a scheduler grows that table by a row per login attempt.
