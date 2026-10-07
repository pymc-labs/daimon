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
