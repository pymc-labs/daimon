# How Daimon handles data

Daimon is self-hosted software. Each deployment has an operator who runs it,
holds the Anthropic API key and controls the database. This page describes
what the software stores, for how long, and what you can do about it.

An operator publishes their own privacy policy and terms for their deployment.
They set [`DAIMON_PRIVACY_POLICY_URL`](docs/configuration.md#daimon_privacy_policy_url)
and [`DAIMON_TERMS_URL`](docs/configuration.md#daimon_terms_url), and the web
app and the privacy panels link to them. Ask the operator about anything
specific to a deployment.

## What is stored

For each tenant (a Discord server, a Slack workspace, or the Microsoft 365
organisation a Teams deployment serves), Daimon stores:

- **Configuration.** The installed agents, environments and skill bindings.
- **Thread mappings.** Which Discord thread, Slack conversation or Teams chat
  maps to which Managed Agents session, so conversations continue.
- **Shared conversation journal.** Daimon's own copy of conversations on its
  provider-neutral path, including stored messages and tool results.
- **Private chat context.** When an admin enables DM conversations, an excerpt
  of the conversation and the 12 most recent private messages, up to 16,000
  characters each. `/dm-me` replaces that copy.
- **Names.** The display name and username each platform last sent for a
  person, and the last name of each channel.
- **Teams installations.** The ID, group ID and name of each team the app is
  added to, and the ID of each 1:1 or group chat it is added to. Channel history is read through Microsoft Graph when a turn needs
  it and sent to the agent with that turn. Daimon keeps no copy.
- **Usage and billing.** Turn counts and credit records that enforce the
  operator's limits. Promo code redemptions record the redeeming account.
- **Security audit.** Tool names, outcomes, reason codes, timestamps and
  tenant, account, platform-user and agent identifiers. Arguments, messages,
  credentials and response bodies are excluded.
- **Credentials.** Bound external credentials, such as a GitHub token,
  encrypted at rest.
- **Feedback and support notes**, and **uploads** you attach to a chat.
- **Hosted artifacts.** Reports and notebooks an agent publishes. Blogs are
  public pages that stay until someone deletes them. Charts in an
  artifact are kept in the operator's object storage.

The conversations themselves run in Anthropic's Managed Agents service, which
holds the full transcript. Daimon's own copies are the journal and private
chat context above.

## What the agent's tools receive

With per-turn context on, each chat message addressed to the agent is also
sent to the agent's MCP servers. That covers servers connected with a shared
token that offer a context tool. The message goes without mentions and with an
opaque id for its thread. Their answers go into the message the agent receives,
like a tool result. This is on by default. An operator turns it off with
[`DAIMON_MCP_CONTEXT__ENABLED`](docs/configuration.md#daimon_mcp_context__enabled).

## How long it is kept

Daimon deletes records past these limits. An operator changes each one with
the setting linked in the table; `0` keeps the records.

| Records | Kept | Setting |
| --- | --- | --- |
| Security audit and agent posts | 90 days | [`DAIMON_RETENTION__SECURITY_AUDIT_DAYS`](docs/configuration.md#daimon_retention__security_audit_days) |
| Web audit and channel changes | 90 days | [`DAIMON_RETENTION__ADMIN_HISTORY_DAYS`](docs/configuration.md#daimon_retention__admin_history_days) |
| Finished jobs, compute and routine output | 90 days | [`DAIMON_RETENTION__JOB_OUTPUT_DAYS`](docs/configuration.md#daimon_retention__job_output_days) |
| Resolved credential and GitHub access requests | 90 days | [`DAIMON_RETENTION__ACCESS_REQUESTS_DAYS`](docs/configuration.md#daimon_retention__access_requests_days) |
| Thread mappings, turn outcomes, the shared journal, private chat context and web threads | 365 days after last activity | [Conversation history kept](docs/features.md#conversation-history-kept) |
| Deleted web threads | 30 days | [`DAIMON_RETENTION__DELETED_THREADS_DAYS`](docs/configuration.md#daimon_retention__deleted_threads_days) |
| Finished task requests | 30 days | [`DAIMON_RETENTION__TASK_CONTINUATIONS_DAYS`](docs/configuration.md#daimon_retention__task_continuations_days) |
| Uploads with no owner | 24 hours | Fixed |
| Feedback and support notes | 365 days | [`DAIMON_RETENTION__FEEDBACK_DAYS`](docs/configuration.md#daimon_retention__feedback_days) |
| Channel names | 365 days after last seen | [`DAIMON_RETENTION__CHANNEL_NAMES_DAYS`](docs/configuration.md#daimon_retention__channel_names_days) |
| Reports | 365 days after last update | [`DAIMON_REPORT__RETENTION_DAYS`](docs/configuration.md#daimon_report__retention_days) |
| A workspace's data after uninstall | 30 days after uninstall | [`DAIMON_RETENTION__ARCHIVED_TENANTS_DAYS`](docs/configuration.md#daimon_retention__archived_tenants_days) |

A workspace whose app is uninstalled (on Teams, from its last team and chat)
is retired after that time: its data, Anthropic sessions, memory, credentials,
skills, reports and notebooks are deleted, in batches that an interrupted run
resumes. Usage not yet settled delays it. Reinstalling before then keeps
everything. Reinstalling during a retirement stops it: what was not yet
deleted is kept, and the Anthropic cleanup still queued is abandoned.

Conversation history is on by default. An operator turns it off with
`DAIMON_FEATURES__CONVERSATION_RETENTION=0`, and a workspace admin can shorten
it in `/settings`.

When a workspace is retired, its billing and usage records are kept for
accounting without the person, account or thread they came from, so a
reinstalled workspace keeps its balance. Notebooks keep the expiry
they were published with, at most 365 days.

Blogs are public pages someone published on purpose, so they have no expiry.
They stay until deleted, or until the workspace that published them is
retired, if the host recorded it. Charts in an artifact sit in the operator's
object storage, which Daimon does not purge. Operators should set a lifecycle
rule on that bucket to match their retention.

## Your rights via `/privacy`

On Discord, run `/privacy` in a server where your account is linked. On Slack,
run `/privacy`. On Teams, send `privacy`; the bot replies in your private chat.
You can:

- **View** who stores what about you, Daimon or Anthropic.
- **Export** a summary, on Slack and Teams. Discord has no export.
- **Delete** your account and its linked personal records.

Deletion covers every server, workspace and organisation linked to your
account in this deployment. Daimon also tries to delete your conversations
from Anthropic. That can fail. If Daimon reports a failure, ask the operator
to check and remove the rest.

Deletion removes your stored name, your private conversations with the
routing records behind them, your skill records and your personal agent
memory. It clears your account and platform-user identifiers from security
logs and your link to promo redemptions. You can read your personal memory
with `/memory mine` and clear it with `/memory forget-mine`.

## What stays after deletion

- **Billing and usage records.** Deleting your account leaves them as they are,
  so they can still hold your platform user ID and conversation ID. They lose
  them only when the whole workspace is retired.
- **Shared conversations.** Messages you wrote in a thread other people still
  use, and the requests you made there. The panel counts them before you
  confirm.
- **Shared agents.** Their memory may mention you. Records they own stay, with
  the link to your account cleared.
- **Anthropic.** Uploaded skill files stay. Transcripts remain if deletion
  fails.
- **Hosted artifacts.** Reports and notebooks expire on their own schedule.
  Blogs stay until deleted. Charts stay in the operator's object storage until
  its lifecycle rule removes them.
- **GitHub.** The authorization stays on your GitHub account. Revoke it in
  GitHub.
- **Feedback and help requests** from a workspace not linked to your account.
  Ask the operator to remove them.
- **Security logs.** The remaining detail expires under the retention above.

Operators include audit records in an export with
`daimon audit list TENANT_UUID --account ACCOUNT_UUID --json`, paging with
`--limit` and `--offset`. The privacy panels do not deliver that JSON.

## Data isolation

Daimon keeps settings and conversation records separate for each server,
workspace and organisation. Your account can link identities across these
places, and privacy summaries and deletion cover the linked identities. One
operator may use the same Anthropic API key for several workspaces.

## Operator responsibility

The operator controls the Anthropic API key, the database and the
infrastructure. Direct data questions about a deployment to its operator, not
to the Daimon project.
