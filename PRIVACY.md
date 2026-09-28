# Privacy Policy

daimon is self-hosted software: each deployment has its own operator who
runs the bot, holds the Anthropic API key, and controls the database. This
document describes what a default daimon deployment stores and the rights
available to end users through the `/privacy` command. It does not cover any
specific operator's practices beyond what the software itself does — for
questions about a particular deployment, contact that server's operator.

## What is stored

For each tenant (a Discord server or Slack workspace that has installed the
bot), daimon stores:

- **Workspace/guild configuration** — the tenant's installed agents,
  environments, and skill bindings.
- **Thread/session mappings** — which Discord thread or Slack conversation
  maps to which Managed Agents session, so conversations can continue across
  messages.
- **Usage and billing events** — turn counts and credit/usage records used
  to enforce the operator's configured usage limits.
- **Security audit metadata** — authenticated main-JWT MCP tool names, authorization outcomes,
  reason codes, timestamps and tenant/account/platform-user/agent identifiers.
  Tool arguments, messages, credentials and response bodies are excluded. These
  records have a default retention age of 90 days, enforced by the operator's
  scheduled `daimon audit prune TENANT_UUID` command. A privacy deletion clears
  account and platform-user identifiers; tenant deletion removes its audit rows.
  Database deletion triggers enforce erasure even for older privacy workers.
  Separate hub OAuth tools are not included in this audit trail. Operators can
  configure a different age or explicitly select indefinite retention.
- **Agent credentials** — any bound external credentials (e.g. a GitHub
  personal access token used by `get_cli_token`), encrypted at rest.

Conversation content lives in Anthropic's Managed Agents service. When an admin
enables DM conversations, daimon also stores the selected workspace, a bounded
source-context excerpt (up to 12 messages / 16,000 characters), and the most recent
12 private user/agent messages (up to 16,000 characters). This supports private
conversation recovery without mixing the history of different workspaces.
Running `/dm` again replaces that local context. `/privacy` account deletion removes
it along with the private routing record. Full JSON export remains unimplemented.

## Your rights via `/privacy`

Every daimon deployment exposes a `/privacy` slash command (Discord) or
equivalent panel (Slack), available to any user, in DM or in a shared
channel. It lets you:

- **View** what is stored about you under the current tenant.
- **Export** your stored data.
- **Delete** your stored data ("delete me"), removing your per-user records
  from that tenant.

The per-user deletion flow clears account and platform-user identifiers from
security audit rows across that account's tenants, including previously unlinked
tenants. Remaining event metadata expires under the operator's retention schedule.
Operators include audit records in privacy exports with `daimon audit list TENANT_UUID
--account ACCOUNT_UUID --json`, paging through all records with `--limit` and
`--offset`. Existing privacy panels do not deliver the audit JSON themselves.

These actions apply to the tenant the command is run in. If you interact
with daimon across multiple servers/workspaces, each tenant's data is
handled independently — see the next section.

## Data isolation

Data is scoped per tenant at the database `tenant_id` layer. Guilds and
workspaces never see each other's data, even though many tenants may share a
single operator's Anthropic API key. There is no cross-guild or
cross-workspace sharing of stored data.

## Operator responsibility

Each daimon deployment is run by its own operator, who controls the
Anthropic API key, the database, and the infrastructure the bot runs on.
Direct any data questions specific to a deployment to that deployment's
operator, not to the daimon project.

Operators who wish to publish their own privacy policy (for example, one
covering jurisdiction-specific commitments) can set
`DAIMON_PRIVACY_POLICY_URL` to point the Policy button at their own page
instead of this document.
