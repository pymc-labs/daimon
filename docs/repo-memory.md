# Repository memory

Repository memory keeps notes and rules about a GitHub repository that every
agent working on it shares. Read this to turn it on or move existing memory
into it. It is off by default.

## What it holds

Entries are keyed by tenant, repository and title. There are two kinds:

- **Directives** are rules. Their full text goes in front of each turn.
- **References** are pointers. Only their titles are listed, and the agent
  reads one when it needs it.

A directive is shared, or personal to the person who saved it. Anyone using
the repository can replace or delete a shared entry. Only the creator sees or
changes a personal one.

The agent uses `repo_memory_list`, `repo_memory_view`, `repo_memory_save` and
`repo_memory_delete`. They act on the agent's working repository, which an
admin approved, so no personal GitHub link is needed. Reading another
repository with `repo=` needs both an agent grant and the person's own GitHub
access.

## Where it applies

- A turn gets the memory again only when it changed since the last turn.
- At most 512 entries and 48 KB go in front of a turn. A directive too long to
  fit becomes a pointer to its full text.
- Saving and deleting are refused in restricted, held and read-only
  conversations.
- Routines, watches and other unattended turns get the shared entries but
  cannot call the tools.
- Shared threads never get anyone's personal directives.
- Erasing an account deletes its personal directives and removes its name from
  shared ones.

## Turn it on

1. Apply the database migration.
2. Freeze writes to the old memory, then run the offline importer,
   `scripts/import_repo_memory.py`. Its docstring holds the input formats.
   Compare the dry-run and applied manifests. Keep exports outside the
   repository.
3. Set
   [`DAIMON_REPO_MEMORY__LEGACY_MCP_SERVER_NAMES`](configuration.md#daimon_repo_memory__legacy_mcp_server_names)
   to the old memory servers' names, so their list and view tools are hidden.
4. Turn on [`DAIMON_REPO_MEMORY__ENABLED`](configuration.md#daimon_repo_memory__enabled)
   on every process.
5. Retire existing conversations, then check the four tools show the imported
   entries.

After native writes begin, rolling back means reconciling those writes first.
Turning the switch off alone brings back stale memory.
