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

## Make it dumb simple

The people who use Daimon are members of a team in a chat app. They are not
engineers, and they are busy. The bar for all that they meet: a tired person
with no technical knowledge can scan it and act.

This is a design rule before it is a wording rule. Plain words do not rescue a
flow that makes a person learn a concept, choose between modes or remember a
command. Decide it when you design the feature, not when you write the card. It
comes before speed of build.

It covers all that a person can meet: cards, buttons, refusals, notices, the web
app, the names of settings and their choices. It also covers what the model reads
and repeats to a person: tool names, tool descriptions, tool results and notices
to the agent. Operators who run Daimon (the CLI, environment settings, logs) are
a different reader; exact technical words are right for them.

1. **Do the hard work, so the person does not.** Daimon decides, remembers and
   recovers where its permissions allow. Where they do not, it gives a button or
   a link to the action and says who can take it. Complexity does not go away:
   we carry it one time, or each person carries it each time.
2. **Do less.** Each concept, mode, setting and step costs every person who meets
   it. Add one only when a person needs the choice, and give it a default that
   works for most people. Name each choice by its effect, so a person can tell
   what changes. More choices make each decision slower, and most people keep
   the default.
3. **Ask late.** Ask for a sign-in, a permission or a decision only when a task
   needs it, and say why in one line. Keep what the decision needs in view (a
   cost, who receives it, what is deleted) and put other detail behind a button.
   A person who has not met the problem does not need its explanation.
4. **One thing, one name, in the person's words.** Use the name a person would
   say ("cloud computer") on cards, in tool descriptions and results, in settings
   and in the docs. Never show a name made for code: a tool name, an argument, a
   setting key, an id, a reason code or JSON. Text that falls back to one is a
   bug. The model repeats our words, and two names read as two things.
5. **Two lines and a button.** One line for what is happening, one line for what
   to do. A button over prose; never ask someone to type what a button can do.
   No " · " separators: each fact gets its own line. Use the same words on every
   platform. People scan a card, and few read it.
6. **A problem ends with a next step.** Say what happened as the person saw it,
   not what broke inside. Then give a step that resolves it, as a button or a
   link where one exists, or name who can help ("Ask an admin or an owner").
   "That didn't work. Try again." gives neither.

Text in the code today that these rules stop:

| Daimon's text | Rule | Instead |
| --- | --- | --- |
| A card titled "Compute launch session" with "Ttl hours: 4" | 4 | "Start a cloud computer for 4 hours?" |
| "Ask an admin to check the image profile it was launched from." | 4 | "The cloud computer could not start, so it is being removed. Ask an admin." |
| "Ask a server admin to set approval for cloud computers to Ask." | 1 | offer the usual time, or give the admin a link to the setting |
| "Ask an admin to run /dm-me enable." | 5 | a button the admin presses |
| A setting with the choices "Off", "Report only", "On" | 2 | choices that say what each one does |

Before you call it done, open the card, panel or reply as the person meets it,
not its source. Ask: can someone who has never heard of this act on it after one
look? Did they have to learn a word, pick a mode or type a command?

These are our rules. They apply "Do the hard work to make it simple" from the
GOV.UK Government Design Principles
(<https://www.gov.uk/guidance/government-design-principles>) and Nielsen Norman
Group's usability heuristics on familiar words, consistency, recognition over
recall and recovery from errors
(<https://www.nngroup.com/articles/ten-usability-heuristics/>).

Errors and notices are never hardcoded text in or beside the agent's reply:

- A failure while a turn runs goes to the agent (`daimon.core.turn.agent_notices`,
  the turn controls), which decides whether and how to tell the person.
- A failure that stops or prevents a turn, or happens with no turn running, is
  the platform's error card (Discord embed, Slack blocks, Teams card), never plain text.
- Any other message of Daimon's own (a confirmation, information, a pointer) is a
  neutral card. News the agent carries on from, like a hand-over, goes to the agent.

`tests/parity/test_agent_notices.py` records the first rule.

`tests/parity/test_plain_words.py`, `tests/parity/test_plain_words_web.py` and
`tests/parity/test_plain_words_parity.py` enforce a list of words and the
separators; Daimon's own prompt carries the setup part
(`defaults/agents/default.yaml`). These tests cover the listed words and
selected messages. They do not read a tool docstring, and they do not prove that
a flow is easy, so check those yourself.

## One rule, every path

Daimon reaches people through Discord, Slack, Teams, the web app, MCP clients
(Claude Code and the plugin), the CLI and the scheduler. Many of our incidents
were a change that reached some of these paths and missed others, or a second
copy of a rule that was left behind. Before you change a behaviour:

1. Find every path that does it. Search for the rule and check its siblings in
   each adapter and each loop. `tests/parity/drivers/`,
   `tests/parity/web_chat_parity.json` and
   `packages/core/daimon/core/platform_words.py` list the chat and web paths.
2. When more than one path follows a rule, put the rule in one core helper and
   call that helper from each path. Do not copy it.
3. Give the rule a contract test that checks every implementation. Find the
   implementations from the code where you can (the AST, a live registry).
   Otherwise list them, and add a failing test for each path that was missed.
   Examples: `packages/adapters/scheduler/tests/test_step_boundary.py` (every
   scheduler step, found by AST) and
   `packages/core/tests/test_github_access_surfaces.py` (a listed set of GitHub
   surfaces, checked through the real token preparation code with a fake GitHub token API).
4. Count sessions that already exist as a path. A change must reach chat threads
   and MCP handles that started before it, not only new ones (`packages/core/daimon/core/session_compat.py`).
5. A sentence that says what works needs a test that proves it. This covers
   agent guidance, seeded prompts, skills, tool docstrings and docs. Map the
   sentence to its test, as `GITHUB_MCP_CLAIMS` does.
6. Record a deliberate difference between paths in a test or allow-list, with
   its reason. A comment alone is not enough. Example:
   `tests/parity/test_teams_deliberate_gaps.py`.

## Features

Anything a person or operator may want on or off is a feature in
`daimon.core.features.registry`: never a new env bool, tenant column or spec flag.
Declare its effect, tiers (per backend), binding, `requires` and `depends_on`.
Read it from the `FeatureSnapshot` that `resolve_features` builds on every session
and turn path; write it with `change_feature`.
`packages/core/tests/features/test_feature_contracts.py` checks reads, writes and
paths. A new feature, or a changed default, tier or platform, must update the "All
features" table in `docs/features.md`: re-run `scripts/generate_feature_reference.py`
(CI runs it with `--check`) and add a `CHANGELOG.md` line.

## Documentation

`docs/` is the public site, written for people who scan it, not for agents.
It grew to 120k words by every PR appending a paragraph; these rules stop that.
`scripts/check_docs.py` enforces the budgets in CI.

**Generated pages.** Settings: `docs/configuration.md` and `.env.example`, from
the field's `description=` (`scripts/generate_config_reference.py`,
`scripts/generate_env_example.py`). MCP tools: `docs/mcp-tools.md`, from the
docstring (`scripts/generate_mcp_tool_catalogue.py`). Features: `docs/features.md`,
from the registry (`scripts/generate_feature_reference.py`). Re-run the generator;
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
| model backends, OpenAI, Gemini | `docs/providers.md` |
| self-hosted harness | `docs/self-hosted-harness.md` |
| upgrades, backups, logs, audit | `docs/operations.md` |
| Slack, Teams, GitHub, MCP servers, plugin | the page under Connect |
| `defaults/` and seeded agents | `docs/defaults.md`, `defaults/README.md` |
| scheduler and routines | `docs/routines.md` |
| metering, pricing, gates | `docs/billing.md` |
| packages, turn path, trust model | `docs/architecture.md` |
| `packages/mux` | `docs/mux.md`, `packages/mux/README.md` |
| anything a user can notice | `CHANGELOG.md` under `[Unreleased]` |
