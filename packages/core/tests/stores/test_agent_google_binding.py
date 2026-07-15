"""Real-DB tests for agent_google_binding store. Phase 19, GH-03."""

from __future__ import annotations

import uuid

import pytest
from daimon.core._models import AgentGoogleBinding
from daimon.core.stores.agent_google_binding import get_agent_google_binding
from daimon.core.stores.domain import AgentGoogleBindingRow
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_get_agent_google_binding_returns_none_when_empty(
    db_session: AsyncSession,
) -> None:
    result = await get_agent_google_binding(
        db_session,
        agent_id=uuid.uuid4(),
    )
    assert result is None, "store returns None for unbound agent"


@pytest.mark.asyncio
async def test_get_agent_google_binding_returns_row_when_populated(
    db_session: AsyncSession,
) -> None:
    agent_id = uuid.uuid4()
    db_session.add(
        AgentGoogleBinding(
            agent_id=agent_id,
            email="user@example.com",
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
        )
    )
    await db_session.flush()

    result = await get_agent_google_binding(db_session, agent_id=agent_id)
    assert result is not None, "store returns a row when binding exists"
    assert isinstance(result, AgentGoogleBindingRow), "store returns Pydantic, not ORM"
    assert result.agent_id == agent_id, "agent_id round-trips"
    assert result.email == "user@example.com", "email round-trips"
    assert result.scopes == ("https://www.googleapis.com/auth/calendar.readonly",), (
        "scopes coerced to tuple[str, ...]"
    )


@pytest.mark.asyncio
async def test_store_returns_pydantic_not_orm(db_session: AsyncSession) -> None:
    agent_id = uuid.uuid4()
    db_session.add(
        AgentGoogleBinding(
            agent_id=agent_id,
            email="x@y.z",
            scopes=["scope-a"],
        )
    )
    await db_session.flush()
    result = await get_agent_google_binding(db_session, agent_id=agent_id)
    assert result is not None
    # Pydantic models are frozen; assignment should fail
    with pytest.raises((TypeError, ValueError)):
        result.email = "other@y.z"  # type: ignore[misc]
