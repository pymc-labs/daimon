# Authoring defaults

How to change the agents, environment and skills every Daimon install is seeded
with, and what happens when you apply the change. Read it if you edit
`defaults/` or run `daimon defaults apply` and `verify`. The one-screen layout
reference is
[defaults/README.md](https://github.com/pymc-labs/daimon/blob/main/defaults/README.md).

## What the tree holds

| Path | One per | Parsed as |
| --- | --- | --- |
| `defaults/agents/<name>.yaml` | file | `AgentSpec` |
| `defaults/agents-optional/<name>.yaml` | file | `AgentSpec`, but never seeded |
| `defaults/environments/<name>.yaml` | file | `EnvironmentSpec` |
| `defaults/skills/<name>/SKILL.md` | directory | `SkillSpec` frontmatter plus the whole directory |
| `defaults/config.yaml` | deployment | `SystemConfigSpec` |

The spec models reject unknown keys, so a typo is a parse error rather than an
ignored field. Field names mirror the Anthropic SDK's create parameters, with no
authoring aliases. The loader enforces two naming rules before anything is
written:

- An agent or environment file's stem must equal its `name`, and a skill
  directory's name must equal its frontmatter `name`.
- Skill names may not contain `anthropic` or `claude` in any case, because the
  provider rejects them.

### Agents

An agent requires `name` and `model`. It accepts `description`, `system`,
`tools`, `mcp_servers`, `multiagent`, `skills`, `skill_repos`, `isolated` and
`template`. A file with `template: true` is a starting point for new agents in
the web app and is never provisioned.
`metadata` is not authorable, because Daimon sets it so that reconciliation
recognises what it owns. Declaring `mcp_servers` without a matching
`{type: mcp_toolset, mcp_server_name: ...}` entry in `tools` is rejected at
parse time.

A `skills:` entry is a reference. Use `{type: custom, skill_id: <name>}` for a
skill in `defaults/skills/`, with the bare authoring name and never a provider
id. Use `{type: anthropic, skill_id: ...}` for a built-in.

An entry may add `platforms: [teams]`, any of `discord`, `slack` and `teams`,
each once, to limit the skill to turns on those platforms. Such a skill is left
off the agent at the provider and out of its fingerprint, so `apply` and
`verify` read the agent as if the entry were absent. Each turn on a listed
platform adds it to the session instead.

- It fills only the room left under the 20-skill limit, and counts before a
  channel's skills.
- Only the Anthropic backend gets it.
- A skill the provider refuses at session start is dropped for an hour, with a
  warning.
- Only `defaults/` agents may use `platforms`.

An entry may also add `requires: microsoft_accounts`, naming an optional
feature. Where the deployment lacks it, the skill is left out and takes no room.
An unknown feature name, or `requires` without `platforms`, fails validation.

### Environments

An environment is a named package manifest and networking policy, and nothing
else. It is not an image, a set of env vars or a tool list.
`defaults/environments/default.yaml` shows the shape: a `config` block whose
`packages` names `pip`, `apt`, `npm` and friends. The default installs the `gh`
CLI.

Daimon fills every package ecosystem with an explicit empty list whenever
`config` is present. Upstream merges per field, so an absent key would keep old
packages. With the empty list, deleting a `pip:` entry and re-applying removes
it.

### Skills

A skill is a directory with `SKILL.md`, plus any `references/`, `scripts/` or
data files. Only `name` and `description` are required in the frontmatter, and
only those two mean anything to Daimon.

Packaging puts every path under a top-level directory named for the skill and
renames any nested `SKILL.md`. It silently drops files whose path has anything
outside `[A-Za-z0-9._/-]`, so keep filenames plain. The limits are 200 files and
28 MiB uncompressed, and exceeding either fails the apply.

### Config and optional agents

`defaults/config.yaml` has exactly two keys, `agent_name` and
`environment_name`. It is the bottom tier of the config cascade
([architecture.md](architecture.md)), used when no thread, channel or tenant
setting applies. Which agents exist is the files in `defaults/agents/`. Which
skills an agent has is its own `skills:` list.

Nothing seeds `agents-optional/`. To opt in, move a file into
`defaults/agents/`, where it is managed like the rest. Or create it per
deployment with `daimon agents create <path>`, which marks it unmanaged so the
sweep leaves it alone.

## Add invocation context fragments

Core adds a `turn_context` block before the user message, chosen by the
trusted caller origin: `chat`, `routine`, `relay` or `handoff`. Default chat
adds nothing. An agent's YAML accepts `context_fragments`, keyed by origin, with
`text` and an optional `mode` (`replace`, the default, or `extend`).

```yaml
context_fragments:
  routine:
    mode: extend
    text: "Include the source timestamps in the result."
  relay:
    text: "Write a concise client-ready answer in Spanish."
```

The configuration is stored in the agent's system field, so it survives upload,
forks and fingerprints. Replacing that field without it removes the overrides.
An empty replacement disables a fragment. Fragments affect prompting only and
grant no permissions.

## Apply defaults

```bash
daimon defaults apply [--dry-run] [--json] [--defaults-root PATH]
```

Apply targets a single local tenant. It has no `--tenant` and no `--force`, so
it is not a fan-out over installs. It loads the whole tree, reconciles skills,
environments and agents in that order, then sweeps in reverse.

Everything that can fail before a write does. Bad YAML, a filename that
disagrees with a `name` and a reference to a missing skill are caught while
loading. A pre-flight step then creates and archives a throwaway agent per
model, and aborts the apply if the model is not accepted. Later failures are
isolated per resource. The pass continues, reports the failure and exits
non-zero at the end.

Registering a tenant fails when its id starts with the same 8 hex characters as
an existing tenant's, because skill titles name a tenant by that prefix. The
error names the workspace, and existing tenants carry on.

**Apply is idempotent.** Agents and environments carry a spec fingerprint, and
the reconciler skips the call when it matches. Skills use the fingerprint table
below.

**The sweep is asymmetric.** An agent or environment that leaves the tree is
archived, never deleted, and a skill is deleted. Only defaults-managed
resources are candidates, so your own agents are never touched. The skill sweep
spares any skill that a live agent pins or a channel rule adds. If it cannot
read the channel rules, it skips the sweep and reports a failure.

Apply also runs at container start and from the Compose `init` service. A
per-tenant version runs lazily when a tag fails to resolve, when the bot joins a
server and on adapter boot. After reconciling seeded agents, it also refreshes
credential guidance in the system prompt of live user agents, leaving other
fields alone.

## Verify defaults

```bash
daimon defaults verify [--json] [--defaults-root PATH]
```

Verify is always a dry run. It walks every ready install and buckets each as
in sync, diverged (naming what would change) or unverifiable (a comparison
failed). Installs not yet ready count as awaiting re-seed, not drift. It exits
non-zero if anything diverged or could not be checked, so it works as a deploy
gate.

## How seeded skills are fingerprinted

The provider cannot tell Daimon whether a skill's content changed. Skills carry
no metadata, and no endpoint returns a version's bytes. Daimon keeps its own
record: a `seeded_skills` row per tenant and skill, holding a content hash and
the provider skill id.

The hash covers each file's archive path and bytes in sorted order, not the zip,
because zips carry timestamps. Files dropped for an unsafe path are excluded.

Apply skips a skill only when a provider skill matches by name, a row exists,
and its id and hash both match. Otherwise it pushes a **new version**. It never
deletes and recreates, because deleting a referenced skill breaks every turn of
the agents that pin it. The row is written after the upload succeeds, and apply
drops the rows of skills no longer in the tree. In practice:

- **You edit `defaults/skills/**`.** The hash changes and apply pushes a new
  version to every install. It overwrites anyone's edits to that skill, with no
  prompt and no `--force`. The only trace is a log line.
- **Someone changes a seeded skill outside `defaults/`.** Chat tools refuse:
  importing under a seeded name fails and `delete_skill` refuses. An edit made
  directly at the provider survives, because the fingerprint still matches.
  Daimon cannot read the content back, so `verify` calls it in sync.
- **No row, but the skill exists.** A new version is pushed unconditionally.

`daimon skills delete <name>` refuses while any agent or channel names the
skill, and lists them. `--force` detaches it everywhere, in every tenant, then
deletes it. Chat's `delete_skill` has no force.

## Settings

`DAIMON_DEFAULTS_ROOT` (default `defaults`) points every service at this tree
([configuration.md](configuration.md#daimon_defaults_root)). The CLI's
`--defaults-root` flag is separate and defaults to the same path.

`DAIMON_MCP__PUBLIC_URL` changes what gets seeded rather than from where. It
merges Daimon's own MCP server and toolset into each seeded agent, so an apply
without it produces agents that differ from a deployed one
([configuration.md](configuration.md#daimon_mcp__public_url)).

The seeded Daimon prompt describes Markdown tables in replies. Discord renders
them as PNG attachments unless the tenant opts out, Slack uses native table
blocks when the tenant enables them, and Teams shows Markdown as it is.
Existing custom agent prompts are unchanged.
