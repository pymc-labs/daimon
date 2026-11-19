"""Test fixtures for daimon-core.

DB fixtures and MA fakes are provided by daimon.testing. Strategy (see
`daimon.testing.db` for the full model):

- One external Postgres (the compose service) and one dedicated test database
  (`daimon_test`), guarded by a name check so misconfiguration fails loudly.
- Each pytest worker owns one schema (`test_w<pid>_<8hex>`) built once per
  session; every connection of `db_engine` has `search_path` pinned to it.
- `db_clean` wipes every ORM table in that schema before each test; `db_session`
  is one checked-out connection on top of it. `@pytest.mark.fresh_schema` gives
  a test a private throwaway schema instead (for tests that run DDL).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from daimon.testing.db import (  # noqa: F401
    db_clean,
    db_committing_engine,
    db_committing_pool,
    db_engine,
    db_nullpool_engine,
    db_schema,
    db_session,
    db_session_factory,
)

from .turn_io_support import headless_identity, turn_io_used  # noqa: F401


@pytest.fixture(autouse=True)
def isolate_skill_reads() -> Iterator[None]:
    """Skill sizes are measured in the background and latest versions are remembered
    process-wide; neither may carry one test's fakes into the next."""
    from daimon.core.skills.latest_versions import LATEST_VERSIONS
    from daimon.core.turn.session_skills import cancel_pending_measures

    LATEST_VERSIONS.clear()
    yield
    cancel_pending_measures()
    LATEST_VERSIONS.clear()
