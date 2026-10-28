# `defaults/`

Authored YAML for the system-default agents, environments, and skills
shipped with every `daimon` deployment. `daimon defaults
apply` reconciles this tree into Managed Agents; each seeded agent/environment
is stamped with a `daimon_managed=true` metadata marker so reconciliation and
sweeps can distinguish seeded defaults from operator-created resources.

Layout:

- `agents/<name>.yaml` — one agent per file. `AgentSpec` shape; filename
  stem must equal the `name` field.
- `environments/<name>.yaml` — one environment per file. `EnvironmentSpec`
  shape; filename stem must equal the `name` field.
- `skills/<name>/SKILL.md` — one skill per directory. The directory name
  must equal the frontmatter `name`. Optional `references/`, `scripts/`,
  etc. are packaged into the zip.

Operator workflow: edit YAML → `daimon defaults apply`.

The `daimon` agent also references Anthropic's built-in `docx`, `pptx` and
`xlsx` skills (`type: anthropic`), limited to Teams with `platforms: [teams]`.
They have no directory here and stay off the agent itself: a Teams turn on
the Anthropic backend adds them to its session (see `docs/defaults.md`).

The default environment installs `gh` for repository work in GitHub App sessions.

`microsoft-365` guides the per-person Microsoft 365 tools
([Teams](../docs/teams.md#microsoft-account-optional)). It is limited to Teams
the same way, so the agent and Slack and Discord sessions never carry it,
and `requires: microsoft_accounts` keeps it off Teams sessions too until
Microsoft accounts are set up.

`skills/lead-intake` provides opt-in Gmail qualification and dossier guidance for
native lead intake. It rejects unsolicited selling and operational mail before
any delivery. Recipient addresses, sales users and CRM settings are tenant-owned.

Lead classification reads each discovered source through `lead_intake_read_source`
before submission; the model receives source text without service credentials.

`workspace-write` ships explicit Gmail/Drive/Docs/Calendar write scripts for an
operator-created `workspace-writer` agent using short-lived delegated Google tokens.
It does not provision credentials or bind the default Daimon agent.

`slides-workflow` packages template copying, text/image edits, validated batches
and PNG rendering for `workspace-writer`. Rendered slides must be inspected before
completion. It shares the same short-lived Google binding as Workspace writes.
