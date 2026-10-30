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

## User-facing text

Anything a person reads in Discord, Slack or Teams (cards, buttons, refusals,
notices, tool results the model relays) is dumb simple:

- One line for what is happening, one line for what to do.
- A button over prose; never ask someone to type what a button can do.
- Everyday words, the same on every platform: no internal terms, no " · " separators.
- A refusal names who can help ("Ask an admin or an owner").

Errors and notices are never hardcoded text in or beside the agent's reply:

- A failure while a turn runs goes to the agent (`daimon.core.turn.agent_notices`,
  the turn controls), which decides whether and how to tell the person.
- A failure that stops or prevents a turn, or happens with no turn running, is
  the platform's error card (Discord embed, Slack blocks, Teams card), never plain text.

`tests/parity/test_agent_notices.py` records the first rule.

`tests/parity/test_plain_words.py` and `tests/parity/test_plain_words_parity.py`
enforce the words and the separators; Daimon's own prompt carries the setup part
(`defaults/agents/daimon.yaml`).

## Documentation

`docs/` is the public site, written for people who scan it, not for agents.
It grew to 120k words by every PR appending a paragraph; these rules stop that.
`scripts/check_docs.py` enforces the budgets in CI.

**Generated pages.** Settings: `docs/configuration.md` and `.env.example`, from
the field's `description=` (`scripts/generate_config_reference.py`,
`scripts/generate_env_example.py`). MCP tools: `docs/mcp-tools.md`, from the
docstring (`scripts/generate_mcp_tool_catalogue.py`). Re-run the generator;
CI runs each with `--check`. Keep descriptions to one short paragraph.

**When to touch a hand-written page.** Only when a fact a reader acts on
changes: a command, flag, required setting, visible behaviour, limit or
recovery step. Most PRs need no docs change. Implementation detail goes in
docstrings and comments; history goes in `CHANGELOG.md` and the PR.

**How to change it.**

- Fix the sentence that is now wrong. Never append a paragraph or a section
  per change; add a section only for a new task or concept, where it belongs
  in the page's flow, not at the end.
- One job per page: `quickstart.md` is a tutorial, Self-host, Connect and
  Customise pages are how-to steps, Concepts explain what and why, Reference
  is generated. Put a fact in one place and link to it; link settings to
  their `configuration.md` anchor instead of restating them.
- Current behaviour only: no "now", "new" or "no longer", no plans, designs
  or PR narrative, no test, conformance or deployment evidence.
- Paragraphs: one idea, key point first, at most 5 sentences and 100 words.
  Sentences under 25 words (CI fails at 40). H2 and H3 only. Numbered lists
  for steps, tables for comparisons, a code block for every command.
- At most 2,000 words of prose a page (quickstart 800, home 500). Over budget
  means cut or split into a new page, never raise the limit.
- The site stands alone: never send a reader to the repo README. The README's
  quickstart mirrors `docs/quickstart.md`; change both together.
- A new page needs a nav entry in `mkdocs.yml` and a line in `docs/README.md`.
  Prose names the product Daimon; `daimon` is the CLI, package and paths.

| Area | Page |
| --- | --- |
| install, Compose, images, storage | `docs/self-hosting.md` |
| model backends, harness, OpenAI, Gemini | `docs/providers.md` |
| upgrades, backups, logs, audit | `docs/operations.md` |
| Slack, Teams, GitHub, MCP servers, plugin | the page under Connect |
| `defaults/` and seeded agents | `docs/defaults.md`, `defaults/README.md` |
| scheduler and routines | `docs/routines.md` |
| metering, pricing, gates | `docs/billing.md` |
| packages, turn path, trust model | `docs/architecture.md` |
| `packages/mux` | `docs/mux.md`, `packages/mux/README.md` |
| anything a user can notice | `CHANGELOG.md` under `[Unreleased]` |
