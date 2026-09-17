"""Shutdown cancellation: a drained turn leaves its orphan marker behind.

``http_service``'s lifespan calls ``dispatcher.drain(timeout=15.0)`` on a
redeploy, which cancels stragglers. A turn cancelled that way froze its
progress card mid-render, and only the NEXT boot's sweep can edit that card
to interrupted — which it can only do if the row's active-turn marker
survives the cancellation. Completion and ordinary failure already rendered
a terminal card, so their markers still clear on the turn's own path.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.teams.lifecycle import FAILURE_MESSAGE
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores.thread_sessions import (
    get_thread_session_by_id,
    list_orphaned_turns,
)
from daimon.core.turn.state import TextBlock, TurnState
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import ENTRA_TENANT_ID, SERVICE_URL, make_dispatch_target, patched_turn_pipeline


@pytest.mark.asyncio
async def test_cancelled_turn_keeps_the_marker_for_the_next_boot_sweep(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """drain() cancels a straggler the way lifespan shutdown does — the marker
    must SURVIVE so ``list_orphaned_turns`` can see the frozen card's row."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    dispatcher, ctx, activity = make_dispatch_target(db_session_factory)

    marked_ids: list[uuid.UUID] = []
    runner_entered = asyncio.Event()
    release = asyncio.Event()  # never set — a turn still in flight at shutdown

    async def _blocking_run_prepared(*args: Any, **kwargs: Any) -> Any:
        runner_entered.set()
        await release.wait()
        raise AssertionError("unreachable — the task is cancelled first")

    with (
        patched_turn_pipeline(marked_ids),
        patch(
            "daimon.adapters.teams.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared,
    ):
        mock_run_prepared.side_effect = _blocking_run_prepared
        await dispatcher.dispatch(ctx, activity)
        # run_prepared_turn is awaited AFTER mark_turn_active commits, so the
        # runner starting is proof the orphan marker is already on the row.
        await asyncio.wait_for(runner_entered.wait(), timeout=10)
        assert marked_ids, "the turn must have passed mark_turn_active"
        # Exactly what the lifespan shutdown path calls.
        await dispatcher.drain(timeout=0.05)

    async with db_session_factory() as session:
        orphans = await list_orphaned_turns(session, platform="teams")
    assert len(orphans) == 1, (
        "a turn cancelled by drain() must leave its active-turn marker so the "
        "next boot's sweep can find the row and edit the frozen card"
    )
    assert orphans[0].id == marked_ids[0]
    assert orphans[0].active_turn_message_id == "m-1"
    assert orphans[0].active_turn_channel_id == SERVICE_URL


@pytest.mark.asyncio
async def test_completed_turn_clears_the_marker(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unchanged path: a turn that reached its terminal render clears its
    marker itself — the sweep has nothing left to do."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    dispatcher, ctx, activity = make_dispatch_target(db_session_factory)

    marked_ids: list[uuid.UUID] = []

    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        state = TurnState(content=[TextBlock(kind="text", text="Hello from Teams!")])
        await lifecycle.on_terminal_success(state)
        return state

    with (
        patched_turn_pipeline(marked_ids),
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as mock_run_turn,
    ):
        mock_run_turn.side_effect = _fake_run_turn
        await dispatcher.dispatch(ctx, activity)
        await dispatcher.drain(timeout=30)

    assert marked_ids, "the turn must have passed mark_turn_active"
    async with db_session_factory() as session:
        assert await list_orphaned_turns(session, platform="teams") == []
        row = await get_thread_session_by_id(session, id=marked_ids[0])
        assert row is not None
        assert row.active_turn_message_id is None
        assert row.active_turn_channel_id is None
        assert row.active_turn_started_at is None
    assert ctx.stream.closed, "the terminal answer must have rendered on the card"


@pytest.mark.asyncio
async def test_failed_turn_clears_the_marker(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unchanged path: an ordinary failure renders the failure card and
    clears the marker — the card is already terminal."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    dispatcher, ctx, activity = make_dispatch_target(db_session_factory)

    marked_ids: list[uuid.UUID] = []

    async def _failing_run_turn(**kwargs: Any) -> Any:
        raise RuntimeError("boom")

    with (
        patched_turn_pipeline(marked_ids),
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as mock_run_turn,
    ):
        mock_run_turn.side_effect = _failing_run_turn
        await dispatcher.dispatch(ctx, activity)
        await dispatcher.drain(timeout=30)

    assert marked_ids, "the turn must have passed mark_turn_active"
    async with db_session_factory() as session:
        assert await list_orphaned_turns(session, platform="teams") == []
        row = await get_thread_session_by_id(session, id=marked_ids[0])
        assert row is not None
        assert row.active_turn_message_id is None
    assert any(FAILURE_MESSAGE in card.model_dump_json() for card in ctx.stream.emitted), (
        "the failure render must have replaced the progress card"
    )


@pytest.mark.asyncio
async def test_internal_cancelled_error_does_not_strand_the_marker(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A CancelledError raised INSIDE a dependency — a timeout, an inner
    task, a library — is not drain() cancellation: nobody called
    ``task.cancel()`` on the dispatcher task, the process keeps running,
    and the boot sweep is startup-only. The marker must clear exactly the
    way an ordinary failure's does."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    dispatcher, ctx, activity = make_dispatch_target(db_session_factory)

    marked_ids: list[uuid.UUID] = []
    runner_entered = asyncio.Event()

    async def _self_cancelling_run_prepared(*args: Any, **kwargs: Any) -> Any:
        runner_entered.set()
        raise asyncio.CancelledError  # dependency-originated, not task.cancel()

    with (
        patched_turn_pipeline(marked_ids),
        patch(
            "daimon.adapters.teams.app.run_prepared_turn", new_callable=AsyncMock
        ) as mock_run_prepared,
    ):
        mock_run_prepared.side_effect = _self_cancelling_run_prepared
        await dispatcher.dispatch(ctx, activity)
        await asyncio.wait_for(runner_entered.wait(), timeout=10)
        assert marked_ids, "the turn must have passed mark_turn_active"
        # drain() is never asked to cancel anything — the task has already
        # ended on the dependency's own CancelledError.
        await dispatcher.drain(timeout=30)

    async with db_session_factory() as session:
        assert await list_orphaned_turns(session, platform="teams") == [], (
            "a self-raised CancelledError must not masquerade as drain "
            "cancellation and leave a frozen marker until some future "
            "process restart"
        )
    assert any(FAILURE_MESSAGE in card.model_dump_json() for card in ctx.stream.emitted), (
        "a dependency-originated CancelledError is an ordinary failure — "
        "the failure card must have replaced the progress card"
    )
