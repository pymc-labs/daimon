# Working in this repo

Layout: `packages/core` (schema, stores, turn pipeline), `packages/adapters/*`
(cli, discord, mcp, scheduler, slack, teams — one platform each, never importing one
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
| add an adapter panel or change a user-facing flow | `docs/architecture.md` | by hand — keep the turn path, the package table and the entry-point list true |
| change compose, deployment or the env a service needs | `docs/self-hosting.md` | by hand |
| add a default skill, agent or environment | `defaults/README.md` | by hand |
| change scheduler dispatch, routine permissions or the failure path | `docs/routines.md` | by hand |
| change metering, pricing or one of the balance and cap gates | `docs/billing.md` | by hand |
| change the `defaults/` tree, `daimon defaults apply`/`verify` or the seeded-skill fingerprint | `docs/defaults.md` | by hand |
| change Slack file delivery or per-user access | `docs/slack.md` | by hand |
| change the plugin's login mounts, commands or install prerequisites | `docs/plugin.md`, `plugin/README.md` | by hand |
| change `packages/mux` or what imports it | `packages/mux/README.md` | by hand |
| change anything a user can notice | `CHANGELOG.md` under `[Unreleased]` | by hand |

`docs/` is the public documentation site: every page in it is published.
It describes behaviour that has shipped, for operators and contributors.
Plans, designs, adoption reviews, refactor notes and anything measured on a
real deployment do not go in `docs/` or the nav; put them in the issue or
the PR description. A note about a scaffold belongs in that package's README.
Prose names the product Daimon; lowercase `daimon` is only the CLI, the
Python package and paths.

Adding a page to `docs/` means two more edits: a line in `docs/README.md`
and a nav entry in `mkdocs.yml`. The strict build fails on a page missing
from the nav.

A PR that touches one of those areas without the matching documentation update
is incomplete. Re-run the generator rather than editing a generated page: CI
runs each one with `--check` and fails on any difference.
