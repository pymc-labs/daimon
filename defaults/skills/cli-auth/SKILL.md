---
name: cli-auth
description: Mint short-lived CLI access tokens via the daimon MCP server's get_cli_token tool.
---

# cli-auth

Use the daimon MCP server's `get_cli_token(service)` tool to mint short-lived
access tokens for external CLIs. Export the result as the appropriate env var
before running CLI commands.

| Service                  | Tool call                  | Env var to export             |
|--------------------------|----------------------------|-------------------------------|
| GitHub                   | `get_cli_token("github")`  | `GH_TOKEN` (or `GITHUB_TOKEN`)|
| Google Cloud / Workspace | `get_cli_token("gcloud")`  | `CLOUDSDK_AUTH_ACCESS_TOKEN`  |

Example shell flow:

```bash
export GH_TOKEN=$(get_cli_token github)
gh repo list
```

The tool requires:

- For `github`: An operator must have already run `daimon auth github` (or a
  Discord user must have authorized via `/agent-setup`). Tokens are minted
  from the persisted PAT.
- For `gcloud`: The agent must be bound to a Google identity
  (`agent_google_binding`) with appropriate scopes, configured by an operator.
  Tokens are short-lived (≈1 hour) impersonated access tokens.

Tokens are minted fresh per call and never cached.
