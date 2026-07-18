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
- `packages/testing/` — shared test fixtures/harness.
- `apps/notebook-host/` — standalone marimo notebook host service.
- `defaults/` — YAML sources for seeded agents, environments, and skills.

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

Tests run against a real Postgres, not an in-memory fake — each test gets its
own schema (`CREATE SCHEMA test_<uuid>` + `DROP SCHEMA ... CASCADE`) for
isolation, so `uv run pytest` is safe to run repeatedly and concurrently
against the same `daimon_test` database.

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
