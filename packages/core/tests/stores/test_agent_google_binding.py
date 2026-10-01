"""Real-DB tests for agent_google_binding store. KEEP-AS-IS(GH-03 is a GitHub issue tracker ref, not an internal designator)"""

from __future__ import annotations

import uuid

import pytest
from daimon.core._models import AgentGoogleBinding
from daimon.core.errors import StoreError
from daimon.core.stores.agent_google_binding import (
    get_agent_google_binding,
    upsert_agent_google_binding,
)
from daimon.core.stores.domain import AgentGoogleBindingRow
from sqlalchemy import func, select
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


@pytest.mark.asyncio
async def test_upsert_agent_google_binding_inserts_row_when_unbound(
    db_session: AsyncSession,
) -> None:
    agent_id = uuid.uuid4()

    result = await upsert_agent_google_binding(
        db_session,
        agent_id=agent_id,
        email="op@example.com",
        scopes=["https://www.googleapis.com/auth/calendar"],
    )

    assert isinstance(result, AgentGoogleBindingRow), "store returns Pydantic, not ORM"
    assert result.agent_id == agent_id, "agent_id round-trips"
    assert result.email == "op@example.com", "email round-trips"
    assert result.scopes == ("https://www.googleapis.com/auth/calendar",), (
        "scopes round-trip as a tuple in declared order"
    )


@pytest.mark.asyncio
async def test_upsert_agent_google_binding_replaces_existing_row_and_bumps_updated_at(
    db_session: AsyncSession,
) -> None:
    agent_id = uuid.uuid4()

    first = await upsert_agent_google_binding(
        db_session,
        agent_id=agent_id,
        email="one@example.com",
        scopes=["scope-a"],
    )
    # Commit so the second upsert runs in a fresh transaction: `func.now()` is
    # transaction-scoped in Postgres, so without a commit both calls would
    # see the same timestamp, as they do across two real CLI-driven sessions.
    await db_session.commit()

    second = await upsert_agent_google_binding(
        db_session,
        agent_id=agent_id,
        email="two@example.com",
        scopes=["scope-b", "scope-c"],
    )

    assert second.email == "two@example.com", "second upsert replaces email"
    assert second.scopes == ("scope-b", "scope-c"), "second upsert replaces scopes"
    assert second.created_at == first.created_at, "created_at is preserved across upsert"
    assert second.updated_at > first.created_at, "updated_at is bumped past the original created_at"

    row_count = await db_session.scalar(
        select(func.count())
        .select_from(AgentGoogleBinding)
        .where(AgentGoogleBinding.agent_id == agent_id)
    )
    assert row_count == 1, "row count for the agent stays 1 after a second bind"


@pytest.mark.asyncio
async def test_upsert_agent_google_binding_round_trips_single_scope(
    db_session: AsyncSession,
) -> None:
    agent_id = uuid.uuid4()

    result = await upsert_agent_google_binding(
        db_session,
        agent_id=agent_id,
        email="solo@example.com",
        scopes=["scope-only"],
    )

    assert result.scopes == ("scope-only",), "single-element scope list round-trips as a tuple"


@pytest.mark.asyncio
async def test_upsert_agent_google_binding_rejects_empty_scopes(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(StoreError, match="scopes"):
        await upsert_agent_google_binding(
            db_session,
            agent_id=uuid.uuid4(),
            email="nobody@example.com",
            scopes=[],
        )
