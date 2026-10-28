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

`toggl-edits` provides named-member update and bulk edits of stopped entries on
`time-editor`, with Reports-verified ownership and no key-owner fallback or admin
operations. Queries cover today minus 365 days through today, using UTC date
bounds. Running entries, very recent edits missing from Reports, and older entries
are unsupported. Running timers must be stopped in Toggl itself because a shared
key cannot verify their owner through Reports.

`image-workflow` generates and edits images through OpenAI and validates PNG
attachments in `/mnt/session/outputs`, using a limited project key on `image-maker`.
The retired optional Gemini Imagen endpoint is refused.

Opt-in agent specs live in `agents-optional/workspace-writer.yaml`,
`agents-optional/time-editor.yaml` and `agents-optional/image-maker.yaml`. After
applying the default skill library, create each with `daimon agents create <path>`.
They are not auto-created and carry no credentials. Restrict their audience before
binding Google or collecting keys; every user of an agent can use its credentials.

Audience restrictions must match the platform. On Discord, configure
`AgentRule.invoker_role_ids` for the permitted roles (admins are checked too;
role-gated agents deny routines). Slack and Teams have no per-user agent gate:
route credential holders only to private channels with restricted membership,
and limit channel reach with `runs_in`. CLI `--agent NAME` is unchecked, so
only trusted operators should have local CLI access. Keep credential holders
out of fleet grants with `worker_agents: "any"` and out of explicit worker-agent
lists for other roots. Outside the Discord role gate, no runtime rule prevents
routines on these agents; the skill's unattended-write prohibition is guidance.
Do not configure those routines.

Google tokens last about one hour; their handoff passes through the session
transcript. Workspace and Slides skills provide exact quoted heredocs that send the
token on script stdin; the scripts do not accept token arguments or save tokens.
Workspace Drive operations and template copying opt into Shared Drive support.
Toggl ownership preflight uses workspace Reports v3 rather than a key-owner-only
read. `sync_workflow_runtime.py --check` fails on missing skill folders, and tests
enforce byte-identical runtime copies.
