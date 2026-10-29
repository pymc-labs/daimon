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
3. Restart the services. The connect routes mount only when these values and the
   public URL are present.
4. Keep the scheduler running. It removes expired connection flows and their
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
missing repo or an oversized skill, end the job. They stay visible in the
binding's `last_sync_error`.

If several active agents in a tenant share one Daimon name, the sync refuses
and `last_sync_error` asks you to archive the duplicates. A new push on the
default branch then starts another pass.

Delivery is at least once. A process death mid-sync can repeat a step. Repeated
passes fetch the branch's current contents and converge. Delivery receipts
expire after 30 days, so a later replay creates one more safe pass.
