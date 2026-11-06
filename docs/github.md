# GitHub

Connect a GitHub App so agents can read and write repositories, and keep their
repo skills in sync when you push. Read this if you run the deployment or
administer an agent's repositories.

## Connect GitHub

Register one GitHub App for the deployment. `<root>` below is
[`DAIMON_MCP__PUBLIC_URL`](configuration.md#daimon_mcp__public_url) without the
trailing `/mcp`. Set the App's user authorization callback to
`<root>/oauth/github/callback` and its setup URL to
`<root>/oauth/github/setup`. GitHub must reach both, so `localhost` works only
through a tunnel.

1. Copy the App's values into `.env`: `DAIMON_GITHUB_APP__APP_ID`, `APP_SLUG`,
   `PRIVATE_KEY`, `CLIENT_ID` and `CLIENT_SECRET`. See
   [configuration](configuration.md#daimon_github_app__app_id).
2. Set [`DAIMON_CRYPTO__KEYS`](configuration.md#daimon_crypto__keys), which
   encrypts the short-lived user tokens.
3. To resync skills on push, set the App's webhook URL to
   `<root>/webhooks/github`, the content type to `application/json`, and
   subscribe to **Push**. Put the webhook secret in
   [`DAIMON_GITHUB_APP__WEBHOOK_SECRET`](configuration.md#daimon_github_app__webhook_secret).
   The route mounts only when it is set, and boot fails without the App ID,
   private key and `DAIMON_CRYPTO__KEYS`.
4. Restart the services. The connect routes mount only when these values and the
   public URL are present.
5. Keep the scheduler running. It removes expired connection flows and their
   encrypted tokens.

### Connect repositories to an agent

Ask an agent to connect GitHub, or run `/github connect` in Discord or Slack. A
server admin, or an owner of that agent, gets a private link bound to that
agent. An owner is not made an admin and can connect repos only to their own
agent. Members can request setup, and their request is recorded for an admin.

On the web page, the person signs in to GitHub and picks repositories. None is
preselected. The page lists repositories the App installation covers and the
signed-in person can see, and says how many it hides. Each selection is checked
again against GitHub at confirmation, and the grants go live then. A repository
the installation does not cover needs a GitHub org owner to add it first.

Every connected repository is read and write for the agent, within the App's
permissions. The person's own GitHub role does not lower that.

In Discord or Slack, `/github` shows the server connection, your personal
GitHub link and waiting requests. Admins and agent owners can also open GitHub
from `/agent-setup` to see an agent's repos. Admins can review requests and
disconnect the server connection.

### Issue links and grants from the CLI

Print an agent-bound link:

```bash
daimon github connect-link --tenant <workspace-uuid> --requester <platform-user-id> --agent <agent-uuid> --agent-name <agent-name>
```

Without `--agent`, the command prints a seven-day, single-use invitation that
connects repos to the workspace, minted on that workspace admin's behalf. An
admin then grants a repository to an agent:

```bash
daimon github grants add --tenant <workspace-uuid> --agent <agent-uuid> --repo <repository-id>
```

Add `--working` to make it the agent's working repo. `grants list` takes
`--tenant` and `--agent`; `grants remove` also takes `--repo`. When the
repository set is unchanged, running sessions rotate tokens in place. A changed
set closes the sessions, so the next turn mounts the new checkouts.

### Replace saved GitHub keys

GitHub App grants replace per-agent saved GitHub keys. Before upgrading from
an installation that has them, preview the loss:

```bash
daimon github retirement-preview
```

It lists the staged grants the migration deletes, and every agent that loses a
saved key or binding with its working repo and whether a live App grant covers
it. An uncovered agent loses GitHub access until an App grant is connected. The
preview stops if an unapproved agent lacks repo coverage.

To accept a loss, set `DAIMON_GITHUB_APP_ONLY_ACCEPTED_LOSS` to a
comma-separated list of agent UUIDs, for both the preview and the Alembic
migration. Without it, the migration stops before deleting rows if any agent
would lose access. It also stops when a retired credential points to more than
one workspace.

The migration asks affected live sessions to start fresh on their next turn.
After upgrading, remove the retired vault credentials:

```bash
daimon github cleanup-legacy-vault
daimon github cleanup-legacy-vault --apply
```

The first command previews, and `--apply` deletes. Run it even after the
sessions restart, because the migration does not revoke vault-side
credentials. GitHub Copilot MCP credentials for `https://api.githubcopilot.com/mcp`
are left alone. During the rollout, an old-image process may briefly reuse a
marked session until it refreshes.

## The agent's GitHub MCP tool

An agent with GitHub's hosted MCP server (`https://api.githubcopilot.com/mcp`)
attached calls it with one GitHub App token from its session. A session needs
more than one token when the agent's repos span two App installations, or one
installation splits into read and write groups or into groups of 500 repos.
Called directly, the MCP tool then reaches only the working repo's group, and
other repos answer 404.

Set [`DAIMON_GITHUB__MCP_ROUTER`](configuration.md#daimon_github__mcp_router)
to `true` to route those sessions through Daimon. It needs
[`DAIMON_MCP__PUBLIC_URL`](configuration.md#daimon_mcp__public_url) and the MCP
service. When it is on, each new session that needs more than one token works
like this:

- Its `github` server points at `<root>/github/mcp`, under the same name, so the
  toolset keeps its permission policy.
- Its own vault holds a bearer that works for that session only. The bearer
  stops working when the session closes or is replaced.
- For each call, Daimon picks the token that covers the repo or owner the call
  names, and forwards the call to GitHub's server.
- A call that names only repos outside the agent's grants uses the working
  repo's token, as a direct session does: GitHub reads a public repo and
  answers 404 for a private one. A call whose granted repos need two tokens is
  refused and asks for one repo at a time. Daimon never uses a token wider than
  the agent's current grants.

Sessions that need one token, and every session created while the setting is
off, call GitHub's server directly, as before.

A session keeps the mode it started with. To roll back, set the setting to
`false` and restart. New sessions then go direct. Routed sessions keep using
the router until they end, so keep the `/github/mcp` route deployed until they
drain.

Before each production release, run this check on staging. Use a test agent
whose repos need two tokens. In a chat turn and in a turn from an MCP coding
key, read one repo from each token group with the `github` MCP tool and with
`gh`. Also ask the tool for a private repo the agent has no grant for, and
confirm that GitHub answers 404, and for a public one, and confirm that it reads.

## How pushes resync skills

A push to a bound repository's default branch refreshes the agent's skills from
that repo. The webhook verifies the request signature, then stores a queue row
and delivery receipt before returning HTTP 200. The database keeps no webhook
body, token or tenant identity.

If saving fails, the webhook returns a non-2xx status. GitHub does not
redeliver failed deliveries by itself, so redeliver the event or reconcile the
repo and branch yourself.

The scheduler runs the queue, so it must be running. Pushes to the same repo
and branch coalesce into one row, and a push during a running sync schedules
one more pass. Each scheduler tick processes up to three rows. Transient
failures and GitHub rate limits retry with backoff, and honour `retry-after`
and rate-limit reset headers. Permanent failures, such as a lost permission, a
missing repo or an oversized skill, end the job and are logged. A retryable
failure keeps the row pending with its last error until a later pass succeeds.

If several active agents in a tenant share one Daimon name, the sync refuses
and logs it until you archive the duplicates. A new push on the default branch
then starts another pass.

The GitHub panel in the agent editor shows the agent's repos, how many skills each
holds and whether the latest sync failed. Its **Resync** button syncs the repo skills
now, unless a sync is already running. Each skill's panel shows where it came from.
A pipeline that publishes the agent can set three metadata keys for the Skills panel
to show: `source_release` (the release name), `source_release_at` (ISO 8601 with an
offset) and `source_release_url` (an https page). Daimon ignores a bad time or URL.

Delivery is at least once. A process death mid-sync can repeat a step. Repeated
passes fetch the branch's current contents and converge. Delivery receipts
expire after 30 days, so a later replay creates one more safe pass.
