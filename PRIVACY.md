# Privacy Policy

daimon is self-hosted software: each deployment has its own operator who
runs the bot, holds the Anthropic API key, and controls the database. This
document describes what a default daimon deployment stores and the rights
available to end users through the `/privacy` command. It does not cover any
specific operator's practices beyond what the software itself does — for
questions about a particular deployment, contact that server's operator.

## What is stored

For each tenant (a Discord server, a Slack workspace, or the Microsoft 365
organisation a Teams deployment serves), daimon stores:

- **Workspace/guild configuration** — the tenant's installed agents,
  environments, and skill bindings.
- **Thread/session mappings** — which Discord thread, Slack conversation or
  Teams chat or channel thread maps to which Managed Agents session, so
  conversations can continue across messages.
- **Teams installations** (Teams only) — the ID, group ID and name of each
  team the app is added to, because Microsoft gives the app no other way to
  list them. Removing the app from a team deletes its record. Channel history
  is read through Microsoft Graph when a turn needs it and sent to the agent
  with that turn; daimon keeps no copy of its own.
- **Names** — the display name and username the chat platform last sent for
  each person with an account in the tenant, and the last name of each
  channel, so the billing panel can name people and channels the platform no
  longer answers for. A privacy deletion removes your stored name in every
  tenant your account is in; without an account, no name is stored.
- **Usage and billing events** — turn counts and credit/usage records used
  to enforce the operator's configured usage limits. Promo code redemptions
  record which account redeemed; a privacy deletion clears that link.
- **Security audit metadata** — names of tools called through Daimon's main
  tool service after sign-in, authorization outcomes, reason codes, timestamps
  and tenant/account/platform-user/agent identifiers.
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
enables DM conversations, daimon also stores the selected workspace, an
excerpt from the conversation (up to 12 messages / 16,000 characters), and the
most recent 12 private user/agent messages (up to 16,000 characters). This
supports private conversation recovery without mixing the history of different
workspaces. Running `/dm` again replaces that local context. `/privacy` account
deletion removes it along with the record linking your private chat to its
workspace. Full JSON export remains unimplemented.

## Your rights via `/privacy`

On Discord, run `/privacy` in a server where your account is linked. On Slack,
run `/privacy` to open the privacy panel. On Teams, send `privacy`; the bot
replies in your private chat. These controls let you:

- **View** a summary of records linked to your account.
- **Export** shows a summary on Slack and Teams. Export is not yet available on
  Discord.
- **Delete** your account and its linked personal records ("delete me").

Account deletion covers every server, workspace and organisation linked to your
account in this copy of Daimon. Daimon also tries to delete conversations
linked to your account from Anthropic. The shared agents and their memory stay.
Chat transcript deletion at Anthropic can fail. If Daimon reports a failure,
ask the person who runs it to check and help remove the remaining transcripts.

Deletion does not remove usage and billing records. These records can still
contain your chat platform user ID and conversation ID. Uploaded skill files at
Anthropic stay. Daimon deletes skill records owned by your account. Records
owned by a shared agent stay, but the link to your account is cleared.
Shared agent memory may still contain information about you. Account deletion
does not remove hosted charts, notebooks or reports. The GitHub
authorization also stays on your GitHub account. Revoke it in your GitHub
settings if you want to remove it there.
Feedback or help requests from a server or workspace not linked to your account
may remain. Ask the person who runs Daimon to help remove them.

The per-user deletion flow clears account and platform-user identifiers from
security logs across that account's tenants, including previously unlinked
tenants. The remaining log details expire under the operator's retention
schedule.
Operators include audit records in privacy exports with
`daimon audit list TENANT_UUID --account ACCOUNT_UUID --json`, paging through
all records with `--limit` and `--offset`. Existing privacy panels do not
deliver the audit JSON themselves.

## Data isolation

Daimon keeps workspace settings and conversation records separate for each
server, workspace and organisation. Your account can link identities across
these places. Privacy summaries and account deletion cover those linked
identities. One operator may use the same Anthropic API key for several
workspaces.

## Operator responsibility

Each daimon deployment is run by its own operator, who controls the
Anthropic API key, the database, and the infrastructure the bot runs on.
Direct any data questions specific to a deployment to that deployment's
operator, not to the daimon project.

Operators who wish to publish their own privacy policy (for example, one
covering jurisdiction-specific commitments) can set
`DAIMON_PRIVACY_POLICY_URL` to point the Policy button at their own page
instead of this document.
