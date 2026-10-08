---
name: cli-auth
description: Obtain CLI access tokens via the daimon MCP server's get_cli_token tool.
---

# cli-auth

Use the daimon MCP server's `get_cli_token(service)` tool to obtain access
tokens for external CLIs. Export the result under the appropriate name
before running CLI commands.

For a GitHub App session, use its mounted repository and session-scoped
`GH_TOKEN` instead. Check the repos available to this agent with
`gh api installation/repositories --jq '.repositories[].full_name'` when a
repo list is needed. Do not name a private repo to someone who cannot see it.
The `get_cli_token("github")` tool is unavailable in App mode. Never ask for a
token in App mode. If `gh` is not installed, use `curl` for GitHub API calls
or the GitHub Copilot MCP tools for repository operations.

| Service                  | Tool call                  | Name to export                |
|--------------------------|----------------------------|-------------------------------|
| GitHub                   | `get_cli_token("github")`  | `GH_TOKEN` (or `GITHUB_TOKEN`)|
| Google Cloud / Workspace | `get_cli_token("gcloud")`  | `CLOUDSDK_AUTH_ACCESS_TOKEN`  |

Call the MCP tool first, then pass its result privately to the shell process
environment before running a command such as `gh repo list`.
`get_cli_token` is an MCP tool, not a shell command.

The tool requires:

- For `github` in legacy mode: the caller must already have a matching GitHub token binding.
  An account-only call reads the account's token; an agent-bound call reads
  that agent's token. Ordinary chat currently uses account-only identity, so
  `request_repo_binding` saving a target agent's token does not make it
  available to this tool. A GitHub App installation alone does not supply it
  either. If access is missing, call `github_connect` to offer the private
  agent-bound App setup link. Use a private PAT form only if the person asks
  for that fallback; do not repeatedly ask them for a token.
- For `gcloud`: the operator must configure deployment Google access and bind
  the agent to a Google identity with `daimon agents bind-google <agent>
  <email> --scopes <scope>`, repeating `--scopes` for additional scopes.
  Tokens are short-lived (≈1 hour) impersonated access tokens.
  The call also needs an agent-bound identity, which ordinary chat does not
  currently provide. Binding the Google identity alone cannot fix a missing
  caller `agent_id`; hand that limitation to the operator.

Each call resolves access afresh. GitHub returns the stored personal token;
Google mints a short-lived impersonated token. Never print either value.
