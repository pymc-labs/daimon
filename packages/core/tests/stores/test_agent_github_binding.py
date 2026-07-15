"""Integration tests for agent_github_binding store — read + write helpers."""

from __future__ import annotations

import uuid

from daimon.core.stores.agent_github_binding import (
    get_agent_github_binding,
    set_agent_github_binding,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_get_agent_github_binding_returns_none_when_unbound(
    db_session: AsyncSession,
) -> None:
    """Day-1 the table is always empty; resolver depends on this returning None."""
    row = await get_agent_github_binding(db_session, agent_id=uuid.uuid4())
    assert row is None, (
        "agent_github_binding lookup for an unbound agent must return None "
        "so the resolver returns None (overlay-only path)"
    )


async def test_set_agent_github_binding_roundtrips(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """set_agent_github_binding writes an overlay row; get_agent_github_binding reads it back."""
    agent_id = uuid.uuid4()
    principal_id = uuid.uuid4()

    async with db_session_factory.begin() as session:
        row = await set_agent_github_binding(session, agent_id=agent_id, principal_id=principal_id)

    assert row.agent_id == agent_id, "returned row must carry the agent_id used in the write"
    assert row.principal_id == principal_id, (
        "returned row must carry the principal_id used in the write"
    )

    # Read back via the read helper to confirm DB round-trip.
    async with db_session_factory() as session:
        read_row = await get_agent_github_binding(session, agent_id=agent_id)

    assert read_row is not None, (
        "set_agent_github_binding must persist the row so get_agent_github_binding finds it"
    )
    assert read_row.agent_id == agent_id, "read row agent_id must match written value"
    assert read_row.principal_id == principal_id, "read row principal_id must match written value"


async def test_set_agent_github_binding_upserts(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """set_agent_github_binding must UPSERT — a second call changes the principal_id."""
    agent_id = uuid.uuid4()
    principal_id_1 = uuid.uuid4()
    principal_id_2 = uuid.uuid4()

    async with db_session_factory.begin() as session:
        await set_agent_github_binding(session, agent_id=agent_id, principal_id=principal_id_1)

    async with db_session_factory.begin() as session:
        row = await set_agent_github_binding(
            session, agent_id=agent_id, principal_id=principal_id_2
        )

    assert row.principal_id == principal_id_2, (
        "second set_agent_github_binding must overwrite the prior principal_id (UPSERT)"
    )

    async with db_session_factory() as session:
        read_row = await get_agent_github_binding(session, agent_id=agent_id)

    assert read_row is not None
    assert read_row.principal_id == principal_id_2, (
        "after upsert, get_agent_github_binding must return the updated principal_id"
    )
