---
name: cli-auth
description: Obtain CLI access tokens via the daimon MCP server's get_cli_token tool.
---

# cli-auth

Use the daimon MCP server's `get_cli_token(service)` tool to obtain access
tokens for external CLIs. Export the result under the appropriate name
before running CLI commands.

| Service                  | Tool call                  | Name to export                |
|--------------------------|----------------------------|-------------------------------|
| GitHub                   | `get_cli_token("github")`  | `GH_TOKEN` (or `GITHUB_TOKEN`)|
| Google Cloud / Workspace | `get_cli_token("gcloud")`  | `CLOUDSDK_AUTH_ACCESS_TOKEN`  |

Call the MCP tool first, then pass its result privately to the shell process
environment before running a command such as `gh repo list`.
`get_cli_token` is an MCP tool, not a shell command.

The tool requires:

- For `github`: the caller must already have a matching GitHub token binding.
  An account-only call reads the account's token; an agent-bound call reads
  that agent's token. Ordinary chat currently uses account-only identity, so
  `request_repo_binding` saving a target agent's token does not make it
  available to this tool. A GitHub App installation alone does not supply it
  either. If access is missing, ask the operator to configure CLI token access
  for the calling identity; do not repeatedly ask the person to bind the repo.
- For `gcloud`: the operator must configure deployment Google access and bind
  the agent to a Google identity with `daimon agents bind-google <agent>
  <email> --scopes <scope>`, repeating `--scopes` for additional scopes.
  Tokens are short-lived (≈1 hour) impersonated access tokens.
  The call also needs an agent-bound identity, which ordinary chat does not
  currently provide. Binding the Google identity alone cannot fix a missing
  caller `agent_id`; hand that limitation to the operator.

Each call resolves access afresh. GitHub returns the stored personal token;
Google mints a short-lived impersonated token. Never print either value.
