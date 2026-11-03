---
name: cli-auth
description: Obtain CLI access tokens via the daimon MCP server's get_cli_token tool.
---

# cli-auth

GitHub App access is scoped to the repos granted to this agent. The working
repo is mounted at `/workspace/<owner>/<repo>` when one is selected. A
session-scoped `GH_TOKEN` may be available for the other granted repos. Use
`github_connect` when a requested repo is not granted. Never ask for a
personal token or use `get_cli_token` for GitHub.

For Google Cloud / Workspace, call `get_cli_token("gcloud")` and export the
result as `CLOUDSDK_AUTH_ACCESS_TOKEN` before running the CLI.

- For `gcloud`: the operator must configure deployment Google access and bind
  the agent to a Google identity with `daimon agents bind-google <agent>
  <email> --scopes <scope>`, repeating `--scopes` for additional scopes.
  Tokens are short-lived (≈1 hour) access tokens.
  Ordinary chat supplies a signed `chat_agent_id`; agent sessions supply
  `agent_id`. Calls without either identity fail closed. A Google binding and
  deployment credentials are required: either authorized-user JSON in
  `google_user_json` plus its account email in `google_user_email`, or a
  service-account key in `google_sa_json` with domain-wide delegation. The user
  credential serves only bindings matching that email, without case sensitivity;
  other bindings require the service account. Both routes use the binding’s
  scopes. With a user credential, the binding’s scopes must be ones the
  sign-in was granted, such as `gmail.modify`, `calendar`, `drive`,
  `documents` and `presentations` (each under
  `https://www.googleapis.com/auth/`); otherwise the refresh fails with
  `invalid_scope`. Report missing setup to the operator.

Google mints a short-lived access token (about one hour). The returned
token enters model context and the session transcript; shell commands containing
it also enter that transcript and may be shell command arguments. Do not echo
or persist it. The workspace-write and slides-workflow skills show a quoted
heredoc that keeps the token out of the script’s own argv and files.
