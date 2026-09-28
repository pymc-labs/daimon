"""Boot-sweep orphan retirement: a frozen progress message is edited to
interrupted and its marker compare-and-cleared — never resumed, never
delivered late.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.teams.boot_sweep import retire_orphaned_turns
from daimon.adapters.teams.lifecycle import INTERRUPTED_MESSAGE
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores.thread_sessions import (
    get_thread_session_by_id,
    list_orphaned_turns,
    mark_turn_active,
)
from daimon.testing.factories import make_thread_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    SERVICE_URL,
    make_dispatch_target,
    patched_turn_pipeline,
)


class FakeSender:
    """Records ``send`` calls like an ``ActivitySender`` would make."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.fail = fail

    async def send(self, activity: Any, ref: Any) -> Any:
        self.calls.append((activity, ref))
        if self.fail:
            raise RuntimeError("service 500")
        return None


async def _seed_orphan(
    session: AsyncSession,
    *,
    service_url: str | None = SERVICE_URL,
    message_id: str = "m-orphan",
    platform: str = "teams",
) -> Any:
    row = await make_thread_session(session, platform=platform, thread_id=CONVERSATION_ID)
    await mark_turn_active(
        session,
        id=row.id,
        active_turn_message_id=message_id,
        active_turn_channel_id=service_url,
        now=datetime.now(UTC),
    )
    await session.commit()
    return row


@pytest.mark.asyncio
async def test_orphan_progress_message_is_edited_and_marker_cleared(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_orphan(db_session)
    sender = FakeSender()

    await retire_orphaned_turns(
        sessionmaker=db_session_factory,
        sender=sender,
        bot_id="bot-1",
        now=datetime.now(UTC),
    )

    assert len(sender.calls) == 1
    activity, ref = sender.calls[0]
    # The edit targets the message id the marker named, on the service_url
    # the conversation lives on — the (service_url, conversation, id) triple.
    assert activity.id == "m-orphan"
    assert ref.service_url == SERVICE_URL
    assert ref.conversation.id == CONVERSATION_ID
    assert INTERRUPTED_MESSAGE in activity.model_dump_json()

    async with db_session_factory() as session:
        refreshed = await get_thread_session_by_id(session, id=row.id)
        assert refreshed is not None
        assert refreshed.active_turn_message_id is None
        assert refreshed.active_turn_channel_id is None


@pytest.mark.asyncio
async def test_sweep_is_idempotent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_orphan(db_session)
    sender = FakeSender()
    for _ in range(2):
        await retire_orphaned_turns(
            sessionmaker=db_session_factory,
            sender=sender,
            bot_id="bot-1",
            now=datetime.now(UTC),
        )
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_orphan_without_service_url_is_still_cleared(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_orphan(db_session, service_url=None)
    sender = FakeSender()
    await retire_orphaned_turns(
        sessionmaker=db_session_factory,
        sender=sender,
        bot_id="bot-1",
        now=datetime.now(UTC),
    )
    assert sender.calls == []
    async with db_session_factory() as session:
        refreshed = await get_thread_session_by_id(session, id=row.id)
        assert refreshed is not None
        assert refreshed.active_turn_message_id is None


@pytest.mark.asyncio
async def test_failed_edit_still_clears_the_marker(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row = await _seed_orphan(db_session)
    await retire_orphaned_turns(
        sessionmaker=db_session_factory,
        sender=FakeSender(fail=True),
        bot_id="bot-1",
        now=datetime.now(UTC),
    )
    async with db_session_factory() as session:
        refreshed = await get_thread_session_by_id(session, id=row.id)
        assert refreshed is not None
        assert refreshed.active_turn_message_id is None


@pytest.mark.asyncio
async def test_other_platforms_and_terminal_state_are_untouched(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slack_row = await _seed_orphan(db_session, platform="slack")
    sender = FakeSender()
    await retire_orphaned_turns(
        sessionmaker=db_session_factory,
        sender=sender,
        bot_id="bot-1",
        now=datetime.now(UTC),
    )
    assert sender.calls == []
    async with db_session_factory() as session:
        refreshed = await get_thread_session_by_id(session, id=slack_row.id)
        assert refreshed is not None
        # The Slack row's marker is not this sweep's business.
        assert refreshed.active_turn_message_id == "m-orphan"
        # And nothing in the row's session/terminal state was touched.
        assert refreshed.status == slack_row.status
        orphans = await list_orphaned_turns(session, platform="teams")
        assert orphans == []


@pytest.mark.asyncio
async def test_sweep_retires_a_turn_cancelled_at_shutdown(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The documented graceful-redeploy path end to end: ``drain()`` cancels
    an in-flight turn, its marker survives, and the next boot's sweep edits
    the frozen progress card to interrupted and clears the marker — touching
    nothing else on the row."""
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)
    dispatcher, ctx, activity = make_dispatch_target(db_session_factory)

    marked_ids: list[uuid.UUID] = []
    runner_entered = asyncio.Event()
    release = asyncio.Event()  # never set — the turn is still running at shutdown

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
        await asyncio.wait_for(runner_entered.wait(), timeout=10)
        await dispatcher.drain(timeout=0.05)

    sender = FakeSender()
    await retire_orphaned_turns(
        sessionmaker=db_session_factory,
        sender=sender,
        bot_id="bot-1",
        now=datetime.now(UTC),
    )

    assert len(sender.calls) == 1, "the cancelled turn's frozen card is the sweep's one target"
    update, ref = sender.calls[0]
    assert update.id == "m-1"
    assert ref.service_url == SERVICE_URL
    assert ref.conversation.id == CONVERSATION_ID
    assert INTERRUPTED_MESSAGE in update.model_dump_json()

    async with db_session_factory() as session:
        assert await list_orphaned_turns(session, platform="teams") == []
        refreshed = await get_thread_session_by_id(session, id=marked_ids[0])
        assert refreshed is not None
        assert refreshed.active_turn_message_id is None
        # The sweep clears only the marker columns — the row's own status is
        # left as it was (a mapping row carries no "terminal" turn status).
        assert refreshed.status == "live"
