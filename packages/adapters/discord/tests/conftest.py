"""Shared fixtures for Discord adapter tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from daimon.testing.db import db_clean as db_clean
from daimon.testing.db import db_engine as db_engine
from daimon.testing.db import db_schema as db_schema
from daimon.testing.db import db_session as db_session
from daimon.testing.db import db_session_factory as db_session_factory
from daimon.testing.factories import make_tenant as make_tenant


@pytest.fixture(autouse=True)
def isolate_card_edits() -> Iterator[None]:
    """Card edits finish in the background; one test's stalled edit must not
    hold the next test's edit to a card with the same id."""
    from daimon.core.posted_controls.lifecycle import cancel_pending_card_edits

    yield
    cancel_pending_card_edits()
