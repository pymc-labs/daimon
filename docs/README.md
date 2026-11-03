# Daimon

Daimon is an open source data science agent for Discord, Slack and Microsoft
Teams. Mention it in a channel and it runs the analysis in a thread, then posts
back charts, models and a notebook that reproduces the result.

These docs are for people who run Daimon on their own infrastructure and for
contributors. To use the hosted version, [add it to your
server](https://daimon.decision.ai/) instead.

## Start here

- **Try it locally:** [Quickstart](quickstart.md) gets a bot answering in
  Discord with Docker Compose.
- **Run it for a team:** [Self-hosting](self-hosting.md) covers a production
  install, then [Model providers](providers.md),
  [Operations](operations.md) and [Fly compute](compute.md).
- **Add a platform or tool:** [Slack](slack.md),
  [Microsoft Teams](teams.md), the [web app](web.md), [GitHub](github.md),
  [MCP servers](mcp-servers.md) and the
  [Claude Code plugin](plugin.md).
- **Shape what it does:** [Defaults](defaults.md) for agents and skills,
  [Routines](routines.md) and [Watches](watches.md) for scheduled work,
  [Direct messages](direct-messages.md),
  [Repository memory](repo-memory.md),
  [Context providers](context-providers.md) and
  [Lead intake](lead-intake.md). [Move routines in](routine-migration.md)
  from another system.

## Understand it

- [Architecture](architecture.md): the packages and how a message becomes a
  turn.
- [Isolation](isolation.md), [Permissions](permissions.md) and
  [Admins](admins.md): who can see and do what.
- [Unattended turns](unattended.md): what runs with nobody watching.
- [Billing](billing.md): how model calls are metered and gated.
- [Long jobs](long-jobs.md): approved work that runs on its own worker.
- [Agent identity](agent-identity.md) and
  [Managed agents](mux.md): how agents appear and run.

## Look something up

- [Configuration](configuration.md): every environment variable.
- [MCP tools](mcp-tools.md): every tool the agent can call.

Both are generated from the code and checked in CI.
