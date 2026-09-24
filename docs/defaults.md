# Authoring defaults

`defaults/` is the YAML tree every deployment seeds into Managed Agents: the
agents a new install gets, the environment they run in, and the skills they
carry. `defaults/README.md` is the one-screen layout reference; this page is
about what happens when you change something in there.

## What the tree holds

| Path | One per | Parsed as |
| --- | --- | --- |
| `defaults/agents/<name>.yaml` | file | `AgentSpec` |
| `defaults/agents-optional/<name>.yaml` | file | `AgentSpec`, but never seeded |
| `defaults/environments/<name>.yaml` | file | `EnvironmentSpec` |
| `defaults/skills/<name>/SKILL.md` | directory | `SkillSpec` frontmatter plus the whole directory |
| `defaults/config.yaml` | deployment | `SystemConfigSpec` |

The models are in `packages/core/daimon/core/specs.py` and the tree is read by
`packages/core/daimon/core/defaults/loader.py`. All three spec models set
`extra="forbid"`, so a typo'd key is a parse error rather than a silently
ignored field, and the field names deliberately mirror the Anthropic SDK's
create-params shapes — what you write is close to what the SDK sees, with no
translation layer in between.

Two naming rules the loader enforces before anything is written: an agent or
environment file's stem must equal its `name` field, and a skill directory's
name must equal the frontmatter `name`. Skill names may not contain
`anthropic` or `claude` in any case, because the provider rejects them.

### Agents

`AgentSpec` requires `name` and `model`, and accepts `description`, `system`,
`tools`, `mcp_servers`, `multiagent`, `skills`, `skill_repos` and `isolated`.
`metadata` is not authorable — it is synthesised at the boundary, which is how
reconciliation later recognises what it owns.

A `skills:` entry is a reference, `{type: custom, skill_id: <name>}` for
something in `defaults/skills/` or `{type: anthropic, skill_id: ...}` for a
built-in. For custom skills `skill_id` is the bare authoring name, never a
provider id; resolution to a real id happens in
`packages/core/daimon/core/defaults/skills.py`, which is the single place
allowed to do it.

One validator catches a failure that is otherwise a confusing upstream 400:
declaring `mcp_servers` without a matching `{type: mcp_toolset,
mcp_server_name: ...}` entry in `tools` is rejected at parse time.

### Environments

An environment is a named cloud config — a package manifest and a networking
policy — and nothing else. It is not an image, not a set of env vars and not a
tool list. `defaults/environments/default.yaml` is the whole shape: a `config`
block whose `packages` names `pip`, `apt`, `npm` and friends.

There is one daimon-specific behaviour authors need to know.
`EnvironmentSpec` fills in every package ecosystem with an explicit empty list
whenever `config` is present. Upstream's environment update merges per field,
so an absent `packages` key would preserve whatever is already there and
removing a dependency from the YAML would never take effect. Because the
validator forces the key, deleting a `pip:` entry and re-applying really does
remove it.

### Skills

A skill is a directory: `SKILL.md` with YAML frontmatter, plus whatever
`references/`, `scripts/` or data files it needs. Only `name` and
`description` are required in the frontmatter and only those two are
interpreted; other keys are preserved but mean nothing to daimon.

`packages/core/daimon/core/skill_zip.py` packages the directory. It rewrites
every path under a top-level directory matching the skill name (the provider
rejects a zip whose top directory disagrees with the manifest), renames any
nested `SKILL.md` so only the root manifest is discoverable, and drops files
whose relative path contains anything outside `[A-Za-z0-9._/-]` — a filename
with a space in it will not ship, and nothing surfaces that to you, so keep
filenames plain. Hard limits are 200 files and 28 MiB uncompressed; exceeding
either fails the apply.

### `defaults/config.yaml`

It has exactly two keys, `agent_name` and `environment_name`, and it binds
nothing else. It is the bottom tier of the config cascade described in
[architecture.md](architecture.md) — the deployment-wide fallback used when no
thread, channel or tenant setting applies. Which agents exist is decided by
which files are in `defaults/agents/`; which skills an agent has is the
`skills:` list in that agent's own file; the model is that file's `model:`.
The shipped `daimon` agent and new-agent fallback use `claude-opus-5-5`.
The optional `dev_agent` stays on `claude-sonnet-5` by design.

### `agents-optional/`

Nothing reads this directory. `defaults/agents/`, `defaults/environments/` and
`defaults/skills/` are the only three the reconciler walks, so an agent parked
in `agents-optional/` is shipped in the repo and seeded into nobody's install.
Opting in means either moving the file into `defaults/agents/` — after which
it is managed and swept like the rest — or creating it per deployment with
`daimon agents create <path>`, which marks it unmanaged so the sweep leaves it
alone.

## `daimon defaults apply`

```
daimon defaults apply [--dry-run] [--json] [--defaults-root PATH]
```

There is no `--tenant` and no `--force`: apply is not a fan-out over installs.
It provisions and targets a single local tenant, loads the whole tree, then
reconciles skills, environments and agents in that order, sweeping in reverse
afterwards. Existing installs pick the change up through the per-tenant path
described below, and `verify` is the command that walks all of them.

Everything that can fail before a write does. Bad YAML, a filename that
disagrees with a `name`, and an agent referencing a skill directory that does
not exist are all caught while loading. After that a pre-flight step creates
and immediately archives a throwaway agent per distinct model to check the
model is accepted, and aborts the whole apply if one is not — better than
discovering it halfway through. Per-resource failures after that point are
isolated: the pass continues, the resource is reported as failed, and the
command exits non-zero at the end.

**It is idempotent, by two different mechanisms.** Agents and environments
carry a spec fingerprint in their provider metadata; when the fingerprint of
what you would write matches what is there, the reconciler skips the call
entirely rather than bumping a version on every boot. Skills use the
fingerprint table described below.

**The sweep is asymmetric.** An agent or environment that disappears from the
tree is archived, never hard-deleted; a skill is hard-deleted. Only resources
stamped as defaults-managed are candidates, so an operator's own agents are
never touched.

Apply is not something you only run by hand. It runs at container start and
from the compose `init` service, and a per-tenant equivalent,
`reconcile_tenant_defaults` in
`packages/core/daimon/core/defaults/provisioning.py`, runs lazily as the
self-heal path when a tag fails to resolve during admission, when the bot
joins a new server, and on adapter boot. That is why the fingerprint skip
matters: without it every one of those calls would rewrite every resource.

## `daimon defaults verify`

```
daimon defaults verify [--json] [--defaults-root PATH]
```

Always a dry run. It walks every ready install, runs the same reconciliation
in comparison mode, and buckets each one as in sync (every resource would be
skipped), diverged (something would change, and it names what), or
unverifiable (a comparison failed). Installs that are not ready yet are
counted separately as awaiting re-seed rather than reported as drift. It exits
non-zero if anything diverged or could not be checked, which is how it is used
as a deploy gate.

## The seeded-skill fingerprint

The provider offers nothing to compare skill content against: skills carry no
metadata, the version counter is opaque, no endpoint returns a version's
bytes, and the folder name is pinned to the manifest name so it cannot carry a
digest either. daimon therefore keeps its own record, in the `seeded_skills`
table — `(tenant, skill name) → content hash, provider skill id`.

The hash is not a hash of the zip file. A zip carries real timestamps, so two
builds of identical content differ byte for byte. `build_skill_zip` instead
digests the same `(archive path, file bytes)` pairs it is about to write, in
sorted order, so the digest cannot describe anything other than what was
uploaded. Files dropped for an unsafe path are excluded from both.

On apply, a skill is skipped only when all three of these hold: a provider
skill matches by name, a `seeded_skills` row exists for it, and that row's
recorded id and content hash both match. Otherwise a **new version** is
pushed — never a delete-and-recreate, because agents pin the latest version
and deleting a referenced skill breaks every one of that agent's turns. The
row is written after the upload succeeds, so a failed upload cannot leave a
fingerprint claiming content was delivered.

What that means in practice when you edit a seeded skill:

- **You edit `defaults/skills/**`.** The local hash changes, the guard fails,
  and apply pushes a new version to every install. If a user had edited that
  skill on their side, your version wins. There is no prompt and no
  `--force`; the only trace is a structured log line.
- **A user edits a seeded skill and `defaults/` is unchanged.** The
  fingerprint still matches, so apply skips it and the user's edit survives.
  daimon never reads the live content back, so it genuinely cannot tell that
  install apart from an untouched one, and `verify` calls it in sync.
- **No fingerprint row, but the skill exists.** A new version is pushed
  unconditionally. This is the case the table was added for: without a local
  record, apply used to adopt whatever was on the provider as correct, which
  left every edit to `defaults/skills/**` undeliverable to installs that
  already had the skill.

## Settings

`DAIMON_DEFAULTS_ROOT` (default `defaults`) is the one setting that points at
this tree, shared by the scheduler, the chat adapters and the MCP routine
tools. The CLI's `--defaults-root` flag is separate and defaults to the same
relative path.

One indirect setting changes what gets seeded rather than from where:
`DAIMON_MCP__PUBLIC_URL` is what merges daimon's own MCP server and its
toolset into each seeded agent, so an apply run without it produces agents
that differ from a deployed one. Both are in
[configuration.md](configuration.md).
