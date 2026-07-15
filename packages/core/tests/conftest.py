"""Test fixtures for daimon-core.

Strategy:

- One external Postgres (the compose service).
- One dedicated test database (`daimon_test`). Checked by a substring guard
  below; misconfiguration fails loudly instead of nuking dev data.
- Session-scoped `db_engine` fixture points at `daimon_test` and verifies
  migrations have been applied (the `alembic_version` row exists).
- Per-test `db_session` creates a fresh schema (`test_<uuid>`), sets
  `search_path` to it, issues `Base.metadata.create_all` into that schema
  (fast — pure DDL, no alembic), yields, then `DROP SCHEMA ... CASCADE`.

DB fixtures and MA fakes are now provided by daimon.testing.
"""

from __future__ import annotations

from daimon.testing.db import db_engine, db_session, db_session_factory  # noqa: F401
