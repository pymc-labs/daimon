# Working in this repo

Layout: `packages/core` (schema, stores, turn pipeline), `packages/adapters/*`
(cli, discord, mcp, scheduler, slack — one platform each, never importing one
another), `packages/mux`, `packages/testing`, `apps/*` (standalone notebook and
report hosts), `defaults/` (seeded agents, environments, skills), `docs/`,
`scripts/`. `CONTRIBUTING.md` has the full tour, the dependency rules and the
dev setup.

Commits follow Conventional Commits (`feat|fix|docs|test|refactor|perf|chore|ci`,
optional scope, lowercase subject); a commit-msg hook rejects anything else.
Keep one logical change per PR, fill in the `## Summary` and `## Checklist`
sections of the PR template, and make `pytest`, `pyright`, `ruff` and
`lint-imports` pass locally before asking for review.

## Documentation to update

Two pages are generated from the code and verified in CI, so editing them by
hand does nothing — change the source and re-run the generator.

| When you… | Update | How |
| --- | --- | --- |
| add or change a setting | `docs/configuration.md`, `.env.example` | generated: `uv run python scripts/generate_config_reference.py` and `scripts/generate_env_example.py`. Write the prose in the field's `description=`. |
| add or change an MCP tool | `docs/mcp-tools.md` | generated: `uv run python scripts/generate_mcp_tool_catalogue.py`. Write the prose in the tool's docstring. |
| add an adapter panel or change a user-facing flow | `docs/architecture.md` | planned, not yet written — until it exists, say so in the PR instead of skipping the thought |
| change compose, deployment or the env a service needs | `docs/self-hosting.md` | by hand |
| add a default skill, agent or environment | `defaults/README.md` | by hand |
| change anything a user can notice | `CHANGELOG.md` under `[Unreleased]` | by hand |

Adding a page to `docs/` also means a line in `docs/README.md`.

A PR that touches one of those areas without the matching documentation update
is incomplete. Re-run the generator rather than editing a generated page: CI
runs each one with `--check` and fails on any difference.
