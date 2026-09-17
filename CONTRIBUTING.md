# Contributing

Thanks for considering a contribution to daimon. This document covers dev
environment setup, the quality gates every PR must keep green, and what we
look for in a pull request.

## Repo tour

- `packages/core/` — `daimon-core` library. Owns schema (Alembic migrations),
  stores, Managed Agents helpers, and the turn pipeline. No adapter imports.
- `packages/adapters/cli/` — `daimon` binary (Typer CLI).
- `packages/adapters/{mcp,discord,slack,scheduler}/` — platform adapters. Each
  owns one platform's I/O, rendering, and auth; adapters never import from
  each other.
- `packages/mux/` — provider-agnostic managed-agent interface.
- `packages/testing/` — shared test fixtures/harness.
- `apps/notebook-host/` — standalone marimo notebook host service.
- `apps/report-host/` — standalone report host service.
- `plugin/` — Claude Code plugin (commands and skills).
- `defaults/` — YAML sources for seeded agents, environments, and skills.
- `docs/` — operator documentation.

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
DAIMON_DATABASE_URL=postgresql+asyncpg://daimon:daimon@localhost:5432/daimon_test \
  uv run alembic upgrade head
export DAIMON_DATABASE__TEST_URL=postgresql+asyncpg://daimon:daimon@localhost:5432/daimon_test
uv run pytest
```

Tests run against a real Postgres, not an in-memory fake. Each pytest worker
owns one schema (`test_w<pid>_<nonce>`), created once per run and dropped at
the end; every ORM table in it is wiped before each test. Because the schema
name carries the worker's pid, concurrent runs and several worktrees can all
point at the same `daimon_test` database safely. Mark a test that runs its own
DDL with `@pytest.mark.fresh_schema` to give it a private throwaway schema
instead, and run `scripts/db/sweep_test_schemas.py` to reclaim schemas left
behind by a killed run.

Install pre-commit hooks once so the gates below run automatically on every
commit:

```bash
uv run pre-commit install
```

## Quality gates

Every PR must keep all four green:

```bash
uv run pytest                              # tests (needs Postgres, see above)
uv run pyright                             # strict type checking
uv run ruff check . && uv run ruff format --check . # lint + format
uv run lint-imports                        # package boundary contracts
```

Pyright runs in strict mode project-wide — new code should carry precise
types rather than `Any`.

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

## Pull request expectations

- Keep diffs focused: one logical change per PR.
- Add or update tests for any behavior change. Prefer real assertions with
  descriptive messages over asserting on shape alone.
- Match the existing code style; don't reformat or refactor unrelated code
  in the same PR.
- Describe what changed and why in the PR description. Link any related
  issue.
- Make sure the four quality gates above pass locally before requesting
  review.
