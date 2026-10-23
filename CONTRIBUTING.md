# Contributing

Thanks for considering a contribution to daimon. This document covers dev
environment setup, the quality gates every PR must keep green, and what we
look for in a pull request.

## Repo tour

- `packages/core/` — `daimon-core` library. Owns schema (Alembic migrations),
  stores, Managed Agents helpers, and the turn pipeline. No adapter imports.
- `packages/adapters/cli/` — `daimon` binary (Typer CLI).
- `packages/adapters/{mcp,discord,slack,teams,scheduler}/` — platform adapters. Each
  owns one platform's I/O, rendering, and auth; adapters never import from
  each other.
- `packages/mux/` — provider-agnostic managed-agent interface.
- `packages/testing/` — shared test fixtures/harness.
- `apps/notebook-host/` — standalone marimo notebook host service.
- `apps/report-host/` — standalone report host service.
- `plugin/` — Claude Code plugin (commands and skills).
- `defaults/` — YAML sources for seeded agents, environments, and skills.
- `docs/` — operator documentation, published as the docs site. Shipped
  behaviour only; plans and designs go in issues.

Dependency rule (enforced by `import-linter` in CI):

- `daimon.core` must not import `daimon.adapters.*`.
- `daimon.adapters.X` must not import `daimon.adapters.Y`.
- `daimon.core._models` (the ORM schema) is private to `daimon.core.stores.**`
  and `daimon.core.defaults.**`. Stores map ORM rows to Pydantic models at the
  boundary; callers never see a session object.

## Dev setup

```bash
uv sync --all-extras --all-packages
docker compose up -d postgres
docker compose exec postgres createdb -U daimon daimon_test_nc_dev
DAIMON_DATABASE_URL=postgresql+asyncpg://daimon:daimon@localhost:5432/daimon_test_nc_dev \
  uv run alembic upgrade head
export DAIMON_DATABASE__TEST_URL=postgresql+asyncpg://daimon:daimon@localhost:5432/daimon_test_nc_dev
uv run pytest
```

Tests run against a real Postgres, not an in-memory fake. Session-scoped fixtures
create process-owned schemas (`test_w<pid>_<nonce>`) and drop them at teardown;
every ORM table in a schema is wiped before each test. Because the schema
name carries the worker's pid, concurrent workers isolate their tables within
the owned local database. Use your configured Postgres credentials and a
`daimon_test_nc_<name>` database for each worktree. Shared fixtures skip plain
local `daimon_test` outside GitHub Actions CI; a green run against that name
does not exercise those database tests. CI alone admits `daimon_test` with
both `GITHUB_ACTIONS=true` and `CI=true` and a registered process-owned schema.
Remote databases and URL query overrides are refused before I/O.
A test whose sessions really commit, or that needs several independent
connections, requests `db_committing_engine`: a pooled engine like production's.
It fails the test that leaves session state on a pooled connection (a `SET`, a
temp table, a `LISTEN`, an advisory lock). Use `db_nullpool_engine` for code that
runs `asyncio.run` per call or takes session-level advisory locks, and set
`DAIMON_TEST_NULLPOOL=1` to run everything on one connection per checkout.
`packages/testing/README.md` has the isolation contract.
Mark a test that runs its own DDL with `@pytest.mark.fresh_schema` to give it the worker's
reusable DDL schema instead: its transaction is rolled back, and a test that commits DDL gets
the schema rebuilt for the next one (`DAIMON_TEST_FRESH_SCHEMA=1` gives each such test a
private throwaway schema). Run `scripts/db/sweep_test_schemas.py` to reclaim schemas left
behind by a killed run.

Install pre-commit hooks once so the gates below run automatically on every
commit:

```bash
uv run pre-commit install
```

## Quality gates

Every PR must keep these four green:

```bash
uv run pytest                              # tests (needs Postgres, see above)
uv run pyright                             # strict type checking
uv run ruff check . && uv run ruff format --check . # lint + format
uv run lint-imports                        # package boundary contracts
```

Pyright runs in strict mode project-wide — new code should carry precise
types rather than `Any`.

CI runs those four across its jobs. Its `lint` job also runs the repo's own
linters — `scripts/lint_discord_modals.py` for Discord modal conventions,
`scripts/lint_anti_patterns.sh` for test anti-patterns, plus the migration
marker and leakage linters described below — and checks that the generated
documentation still matches the code:

```bash
uv run python scripts/generate_env_example.py --check      # .env.example
uv run python scripts/generate_config_reference.py --check # docs/configuration.md
uv run python scripts/generate_mcp_tool_catalogue.py --check # docs/mcp-tools.md
```

Those three pages are written by generators, so a change to a settings field's
`Field(description=...)` or to an MCP tool's docstring is a change to their
source: re-run the generator without `--check` and commit the result, rather
than editing the page. `CLAUDE.md` has the table of which change means which
generator, and which pages are maintained by hand.

CI splits the Postgres-backed pytest run into parallel jobs, and the largest
suites (`core`, `mcp`, `discord`) into shards by test file with
`-p daimon.testing.ci_shard --ci-shard-by=file --ci-shard-suite=S --ci-shard-index=N --ci-shard-count=M`.
Files are placed longest-first on the least-loaded shard using the per-file
seconds in `packages/testing/daimon/testing/ci_durations.json`; a file missing
from it still runs, on the shard its path hashes to. Refresh the table from a
run's junit artifacts with `uv run python scripts/generate_ci_durations.py --run <run-id>`.
Each job runs `-n 4 --dist worksteal` (the four vCPUs of a hosted runner;
Discord keeps `-n 2`), so a failure names the failing area in the job list. Add
a shard by adding a leg to the `pytest-db` matrix (or the `pytest-discord` one)
in `.github/workflows/ci.yml`. The `shard-check` job
runs `uv run python scripts/check_ci_shards.py`, which collects each split suite
once (`--collect-only`, no database), places its files the way CI will, and
fails if a shard is empty or the shards omit or repeat a test, so a new test
file lands in exactly one shard without any edit.

The turn-relevant suites (`packages/core/tests/turn`, `tests/parity`, the
Slack, Discord, Teams and scheduler adapters, `daimon run`, and the non-chat
turn files: headless, routines, smoke, workspace transfer and session
preparation) also run on a second, blocking `pytest-mux-leg` job
with `DAIMON_TURN__PATH=mux` and `DAIMON_TURN__CHANNEL_BACKENDS=true`; its suites
carry a `-mux` suffix. Fixtures read the leg through `daimon.testing.turn_leg`
(`deps_for` and the parity drivers), so run a suite locally on the mux leg by
setting those two variables. The mux leg is a ratchet: `tests/mux_leg_known_failures.txt` lists the
failures that exist today and `-p daimon.testing.mux_leg_known` marks them strict
expected failures, so a new failure fails the leg and a listed test that starts
passing must be removed from the list. It is not a prerequisite of the staging
deploy, because legacy is the production default. A test that only holds on mux builds its deps with
`deps_for_pinned_mux`; the rest must hold on both legs.

The blocking `pytest-staging-config` job also runs focused turn checks in two
configurations. Its enabled leg sets `DAIMON_TURN__PATH=mux`,
`DAIMON_TURN__CHANNEL_BACKENDS=true`, `DAIMON_TURN__AGENT_BACKENDS=true`, and
`DAIMON_FLEET__ENABLED=true`; its legacy leg sets the path and all three switches
off. The tests verify the selected platform turn path, native agent counterpart
proof, and fleet grant admission. Both legs must pass before staging deploys.

The pre-commit hooks from the setup above run the linters and the generator
checks on `git commit`; `pytest` stays a manual step.

`tests/platform_contract/` checks every Slack and Discord payload offline, and against the real
platform validators when you opt in with `pytest -m platform_contract` and QA bot tokens (see its
README); the opt-in half never runs in CI.

### Production comments

Explain invariants and causal reasons in product comments and docstrings. Keep
ticket numbers, review rounds, lane names and checklist labels in commit or PR
provenance.

### Migration downgrade-safety markers

Every file under `packages/core/alembic/versions/` must carry a
`downgrade: safe | destructive | unsupported` line in its module docstring:

- `safe` — `downgrade()` reverses the migration cleanly, no data loss.
- `destructive` — `downgrade()` reverses the migration but loses data.
- `unsupported` — `downgrade()` raises `NotImplementedError`.

`alembic revision` emits an invalid `downgrade: TODO-declare
(safe|destructive|unsupported)` placeholder — replace it with the real
value. `scripts/lint_migrations.py` (wired into pre-commit and CI)
AST-cross-checks the declared value against the `downgrade()` body, so
declaring `unsupported` requires actually raising `NotImplementedError`.

### Designators in comments

Comments and test names cite short ids: `D-14` is an architecture decision,
`T-05-04a` a threat-model item, `SYNC-03` and friends a requirement. They
are traceability anchors, not tickets you need access to — the sentence
around one always states the rule it stands for, and that sentence is the
part that has to hold. Keep an id when you move the code it annotates;
never add one for a rule you have not written down in prose.

`scripts/lint_designators.sh` (pre-commit and CI) guards the other
direction: this repository is public, so it must not name the maintainers'
private planning repository, production hostnames or cloud project ids.
Describe the behaviour instead.

## Pull request expectations

- Keep diffs focused: one logical change per PR.
- Add or update tests for any behavior change. Prefer real assertions with
  descriptive messages over asserting on shape alone.
- Match the existing code style; don't reformat or refactor unrelated code
  in the same PR.
- Describe what changed and why in the PR description. Link any related
  issue.
- Make sure the quality gates above pass locally before requesting review.
