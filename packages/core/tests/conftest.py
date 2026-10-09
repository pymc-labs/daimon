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
    db_engine,
    db_nullpool_engine,
    db_schema,
    db_session,
    db_session_factory,
)


@pytest.fixture(autouse=True)
def isolate_card_edits() -> Iterator[None]:
    """Card edits finish in the background; one test's stalled edit must not
    hold the next test's edit to a card with the same id."""
    from daimon.core.posted_controls.lifecycle import cancel_pending_card_edits

    yield
    cancel_pending_card_edits()
