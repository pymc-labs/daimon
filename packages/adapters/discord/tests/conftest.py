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


@pytest.fixture(autouse=True)
def isolate_agent_faces() -> Iterator[None]:
    """Creating an agent starts its face render in the background; one test's
    render must not write into the next test's database or back-off state."""
    from daimon.core.agent_identity import cancel_pending_agent_faces

    yield
    cancel_pending_agent_faces()
