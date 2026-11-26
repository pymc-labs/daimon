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

CI's `conventions` job enforces the Migrations, Changelog and Documentation
rules below. When it fails, read its message and the section it names on
latest main before changing anything.

Staging deploys are automatic and debounced: merge freely. A SHA deploys once its
CI passes and `STAGING_QUIET_MINUTES` (default 5, a floor for fast runs) have
passed, unless `main` has moved past it by then; a burst deploys once, from its
last commit. If that last commit fails CI, the green ones before it are skipped
and staging stays put until the next green push, so fix forward. Production is
still `promote.yml`.

## Make it dumb simple

The people who use Daimon are members of a team in a chat app. Most are not
engineers, and all are busy. The bar: a tired person with no technical knowledge
can start, get a useful result and continue.

Design the path before the screen. A path is all that a person does between
wanting a thing and having it: each press, choice, typed command, wait, sign-in,
visit to another page and new word. Plain words do not rescue a path that is
long or that a person must learn. Decide the path when you design the feature.
It comes before speed of build.

1. **Make the first useful result easy.** Design so that, after Daimon is
   installed, a new member can make a first request in their own words and get a
   useful result. Keep shared setup with admins, who do it one time for all. Ask
   a member only for the access or the choice that their task needs. Let Daimon
   pick the agent, the tool and the settings, within what the admins allow.
2. **Count the steps, then remove them.** List the path. Each press, choice,
   command, wait, other page and new concept costs every person who takes it.
   Remove it, give it a default that works for most people, or let Daimon do it.
   Name each choice that stays by its effect, so a person can tell what changes.
   Complexity does not go away: we carry it one time, or each person carries it
   each time.
3. **Ask late, in the place they are.** Ask for a sign-in, a permission or a
   decision only when the task needs it, with one press in the same conversation
   where one is possible. Say why in one line. Keep what the decision needs in
   view: a cost, who receives it, whose account is used, what is deleted. Put
   other detail behind a button. A person who has not met the problem does not
   need its explanation.
4. **Continue after a block.** When the access or the approval arrives, make
   Daimon continue the waiting request by itself, within its current
   permissions, so the person does not ask again. Do not restart work that was
   cancelled, replaced or expired; say what to do next. When a different person
   must act, give them the request and tell the requester who has it, as
   `daimon.core.github_request_decisions` does. A new block does not get this by
   itself: build it and test it.
5. **End each path in view.** Deliver the result where the person asked, or in
   the place they named. If it must go somewhere private, say so. End a problem
   with what happened, as the person saw it, and a step that resolves it: a
   button, a link, or who can help ("Ask an admin or an owner"). Work that ends
   where nobody looks, and "That didn't work. Try again.", are not ends. The
   rules below say who carries a problem to the person.
6. **Then the words.** Use one name for one thing, the name a person would say
   ("cloud computer"), on every platform. Keep names made for code out of what a
   person reads: a tool name, an argument, a setting key, an id, a reason code,
   JSON. Text that falls back to one is a bug. The model repeats our words, and
   two names read as two things. In a card or a notice, use one line for what is
   happening and one line for what to do, then the facts a decision needs. A
   button over prose; never ask someone to type what a button can do. No " · "
   separators: each fact gets its own line. A card is a piece of UI, not a
   message from an assistant: no greeting, no filler, space between separate
   facts. Send no card that repeats the agent's answer or reports a step nobody
   must act on; update one progress card instead of adding more.

Rule 6 covers what a person reads: cards, buttons, refusals, notices, the web
app, the names of settings and their choices. It also covers the prose the model
reads and repeats to a person: tool descriptions, tool results and notices to
the agent. Tool names, arguments, and the ids and JSON the model needs stay as
they are; put the person's word for the thing beside them. Operators who run
Daimon (the CLI, environment settings, logs) are a different reader; exact
technical words are right for them.

Before you call it done, list each path you changed, from its start to its
result, as a new person takes it. Count what they must learn, choose, type, wait
for and do again. Then take the path in the app; if you cannot, say so in the PR.

These are our rules. They apply "Start with user needs" and "Do the hard work to
make it simple" from the GOV.UK Government Design Principles
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

## Migrations

Migrations form one linear chain in `packages/core/alembic/versions/`, and
`packages/core/alembic/head.txt` names its head, so two branches that add a
migration conflict there instead of breaking `main`.

1. A migration ships in the same PR as the code that uses it. Never revert a
   migration.
2. Declare `downgrade:` and `schema: expand|contract`. `expand` only adds
   (tables, nullable or defaulted columns, non-unique indexes, new functions),
   so old and new code both run on it. Anything else is `contract`: it must not
   remove or change anything the previous release still uses.
3. After merging or rebasing `main`, run `scripts/rechain_migrations.py`. It
   renumbers only your new migrations and rewrites `head.txt`. If `head.txt`
   conflicts, take `main`'s copy, finish the merge, then run it.
4. Never rename or edit a migration that is on `main`.
5. Tests load a migration with `load_migration("<slug>")` and never assert its
   `down_revision`. `test_migration_chain.py` reads the chain from the files.

## Features

Anything a person or operator may want on or off is a feature in
`daimon.core.features.registry`: never a new env bool, tenant column or spec flag.
Declare its effect, tiers (per backend), binding, `requires` and `depends_on`.
Read it from the `FeatureSnapshot` that `resolve_features` builds on every session
and turn path; write it with `change_feature`.
`packages/core/tests/features/test_feature_contracts.py` checks reads, writes and
paths. A new feature, or a changed default, tier or platform, needs a changelog
fragment. Add a spec, or an entry in a test's per-feature table, next to its
closest sibling, never at the end.

## Changelog

Anything a user can notice gets a fragment: `changes/<topic>.md` with one
`- **Bold lead.**` paragraph in the style of `CHANGELOG.md`. Never edit
`CHANGELOG.md` in a change; `scripts/changelog.py fold` moves fragments into it.

## Documentation

`docs/` is the public site, written for people who scan it, not for agents.
It grew to 120k words by every PR appending a paragraph; these rules stop that.
`scripts/check_docs.py` enforces the budgets in CI.

**Generated pages.** `docs/configuration.md`, `docs/mcp-tools.md` and
`docs/features.md` are built by the docs workflow from `scripts/generate_*.py`
(a setting's `description=`, a tool's docstring, the feature registry); never
commit them. `.env.example` is committed: re-run
`scripts/generate_env_example.py`, and on a merge conflict take either side and
re-run it. Keep descriptions to one short paragraph.

**When to touch a hand-written page.** Only when a fact a reader acts on
changes: a command, flag, required setting, visible behaviour, limit or
recovery step. Most PRs need no docs change. Implementation detail goes in
docstrings and comments; history goes in a changelog fragment and the PR.

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
| anything a user can notice | a fragment in `changes/` |
