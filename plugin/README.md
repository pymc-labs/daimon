# daimon plugin for Claude Code

Talk to the daimons in your Slack workspace and Discord servers from inside Claude Code.
The plugin connects to two hosted MCP servers — `daimon-slack` and `daimon-discord` — each
behind its own OAuth login, so a daimon answers as you: it reads only the channels you can
see and spends that workspace's credit.

## Install

Once published to a plugin marketplace:

```
claude plugin install daimon
```

To try it locally from a checkout of this repo, without publishing:

```
claude --plugin-dir ./plugin
```

## Log in

The plugin declares `daimon-slack` and `daimon-discord` as HTTP MCP servers; neither
carries credentials. Run `/mcp` inside Claude Code and authenticate each server you want
to use — that starts the OAuth login for that platform and stores the resulting token
locally. A server you never authenticate simply has no daimons to list.

## What's included

- **`daimon-context` skill**: activates when you ask about team context, project status,
  decisions, or discussions that live in Slack or Discord, or name a daimon directly. It
  lists the daimons you can reach, picks the ones likely to have the answer, asks them,
  and merges the replies with their source workspace.
- **`/daimon-status` command**: lists every connected `daimon-*` server and the daimons
  reachable through it, without asking any of them a question. Use it to check what's
  connected before asking something.

## Self-hosted deployments

By default the plugin points at `https://daimon.decision.ai`. If you run your own daimon
deployment, set `DAIMON_MCP_URL` to your server's origin before starting Claude Code:

```
DAIMON_MCP_URL=https://your-daimon-host.example claude --plugin-dir ./plugin
```

`DAIMON_MCP_URL` must be an origin only — scheme and host, no path and no trailing
`/mcp` — since the plugin appends `/slack/mcp` and `/discord/mcp` itself.
