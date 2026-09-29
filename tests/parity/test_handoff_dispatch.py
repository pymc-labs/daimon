"""Scenario: a queued task-continuation is dispatched at most once, at the
platform entry point.

A `hand_off_task` call with `continuation` set writes a `thread_agent_bindings`
row (`kind='handoff'`) and a `task_continuations` row in the same transaction
(`packages/adapters/mcp/.../tools/task_continuity.py`), but dispatches
nothing itself. The adapter picks the row up the next time ANY turn completes
in that thread and runs the destination's first turn through the ordinary
`admit -> bind_session -> run_prepared_turn` path.

This scenario writes the binding/continuation rows directly via the core
stores rather than driving the real `hand_off_task` MCP tool -- the tool's
own decision logic (refusals, the same-agent check, the uncommitted-work
question) is covered by `packages/adapters/mcp/tests/tools/test_task_continuity.py`;
what this file proves is the adapter's dispatch wiring: claim-once, exactly
one follow-up turn, billed once. The binding's destination is deliberately
the SAME concrete agent the thread already had (`AGENT_ID`) rather than a
second agent -- `session_compat` then decides a plain reuse with no
replacement, so the scenario exercises dispatch in isolation from the
(separately, core-side tested) workspace-transfer/replacement machinery.

`thread.history()` -- a Discord-API-only concern the dispatch module uses to
detect a superseding human message -- is boundary-stubbed here the same way
`create_session` and `build_context_xml` already are for this driver
(`DiscordDriver`'s own docstring): the platform mocks in this suite are plain
`MagicMock(spec=discord.Thread)` objects with no `.history` behavior wired up.
Teams runs the Discord half with its own stub (`unsuperseded_continuations`).
"""

from __future__ import annotations

import uuid
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from unittest.mock import AsyncMock, patch

import pytest
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.task_continuations import get_continuation, record_continuation
from daimon.core.stores.thread_agent_bindings import upsert_responder_binding
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import AGENT_ID, AGENT_TEXT, build_turn_router
from .drivers.discord_driver import DiscordDriver
from .drivers.protocol import thread_ids
from .drivers.slack_driver import SlackDriver
from .drivers.teams_driver import TeamsDriver, unsuperseded_continuations


@pytest.mark.parametrize("platform", ["discord", "teams"])
async def test_handoff_continuation_dispatches_exactly_one_follow_up_turn(
    platform: Literal["discord", "teams"],
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, user_id, parent_channel_id, thread_id = thread_ids(
        platform, workspace=900003001, user=555000333, thread=300000
    )

    tenant = await make_tenant(db_session, platform=platform, workspace_id=workspace_id)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform=platform, external_id=user_id
    )
    await db_session.commit()

    # Three turns run against the SAME MA session id (turn 1 creates it,
    # turns 2 and 3 reuse it) and `usage_events` idempotency keys on
    # `(managed_session_id, event_id)`: without fresh event ids per stream
    # open, three turns would silently collapse to one billed row.
    router = build_turn_router(str(tenant.id), fresh_event_ids=True)
    driver = DiscordDriver() if platform == "discord" else TeamsDriver()

    # Turn 1: an ordinary mention, no binding/continuation yet -- establishes
    # the live session the handoff will later reuse.
    posted_1 = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=thread_id,
        user_id=user_id,
        text="hello",
    )
    assert any(AGENT_TEXT in p for p in posted_1), "turn 1 should post the agent's reply"

    # A task handoff (destination == the same agent already answering here,
    # see module docstring) carrying work to continue, recorded the way
    # `hand_off_task` records it -- one binding, one pending continuation.
    idempotency_key = uuid.uuid4()
    requested_work = "continue drafting the report"
    async with db_session_factory() as session:
        await upsert_responder_binding(
            session,
            tenant_id=tenant.id,
            platform=platform,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            responder_ma_agent_id=AGENT_ID,
            responder_name="test-agent",
            created_by_account_id=principal.account_id,
            now=datetime.now(UTC),
        )
        await record_continuation(
            session,
            tenant_id=tenant.id,
            platform=platform,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            requester_account_id=principal.account_id,
            requester_external_user_id=user_id,
            target_ma_agent_id=AGENT_ID,
            target_name="test-agent",
            reason="task_handoff",
            idempotency_key=idempotency_key,
            requested_work=requested_work,
        )
        await session.commit()

    # Turn 2: another mention in the same thread. The handoff binding is now
    # active (destination unchanged), so this turn runs and completes
    # normally; its completion is what triggers dispatch of the pending
    # continuation, still inside the same `on_message` call.
    unsuperseded: AbstractContextManager[object] = (
        patch(
            "daimon.adapters.discord.continuation_dispatch._latest_human_message_at",
            new_callable=AsyncMock,
            return_value=None,
        )
        if platform == "discord"
        else unsuperseded_continuations()
    )
    with unsuperseded:
        posted_2 = await driver.dispatch_turn(
            sessionmaker=db_session_factory,
            router=router,
            tenant_id=tenant.id,
            workspace_id=workspace_id,
            channel_id=thread_id,
            user_id=user_id,
            text="still there?",
        )

    # Exactly one follow-up turn ran: the agent's reply appears twice in what
    # this single on_message call posted (turn 2's own answer, plus the
    # dispatched continuation's answer) -- never a third time.
    assert sum(1 for p in posted_2 if AGENT_TEXT in p) == 2, (
        "turn 2 and the dispatched continuation should each post the agent's reply once"
    )

    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=idempotency_key)
    assert row is not None
    assert row.status == "delivered", "the continuation should settle as delivered, not skipped"

    async with db_session_factory() as session:
        events = await usage_events.list_for_tenant(session, tenant_id=tenant.id)
    # Turn 1 + turn 2 + the dispatched continuation's own turn -- three billed
    # turns, never a fourth (a double-dispatch would bill a fourth here).
    assert len(events) == 3, f"expected exactly 3 billed turns, got {len(events)}"


async def test_slack_handoff_continuation_dispatches_exactly_one_follow_up_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Slack half of the same scenario -- proves
    `daimon.adapters.slack.continuation_dispatch.dispatch_pending_continuations`
    wired into `SlackApp._run_thread_turn`'s completion path: claim-once,
    exactly one follow-up turn, billed once. Mirrors the Discord scenario
    above (destination == the same concrete agent already answering here, so
    `session_compat` decides a plain reuse -- this file proves dispatch
    wiring, not workspace-transfer/replacement, which is core-side tested).
    """
    workspace_id = "T_HANDOFF_DISPATCH_PARITY"
    user_id = "U_HANDOFF_DISPATCH_PARITY"
    thread_id = "9200000010.000001"
    parent_channel_id = "C_HANDOFF_DISPATCH_PARITY"

    tenant = await make_tenant(db_session, platform="slack", workspace_id=workspace_id)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    principal = await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="slack", external_id=user_id
    )
    await db_session.commit()

    # Three turns run against the SAME MA session id (turn 1 creates it,
    # turns 2 and 3 reuse it) and `usage_events` idempotency keys on
    # `(managed_session_id, event_id)`: without fresh event ids per stream
    # open, three turns would silently collapse to one billed row.
    router = build_turn_router(str(tenant.id), fresh_event_ids=True)
    driver = SlackDriver()

    # Turn 1: an ordinary mention, no binding/continuation yet -- establishes
    # the live session the handoff will later reuse.
    posted_1 = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=parent_channel_id,
        user_id=user_id,
        text="hello",
        thread_ts=thread_id,
    )
    assert any(AGENT_TEXT in p for p in posted_1), "turn 1 should post the agent's reply"

    # A task handoff (destination == the same agent already answering here),
    # recorded the way `hand_off_task` records it -- one binding, one pending
    # continuation.
    idempotency_key = uuid.uuid4()
    requested_work = "continue drafting the report"
    async with db_session_factory() as session:
        await upsert_responder_binding(
            session,
            tenant_id=tenant.id,
            platform="slack",
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            responder_ma_agent_id=AGENT_ID,
            responder_name="test-agent",
            created_by_account_id=principal.account_id,
            now=datetime.now(UTC),
        )
        await record_continuation(
            session,
            tenant_id=tenant.id,
            platform="slack",
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            requester_account_id=principal.account_id,
            requester_external_user_id=user_id,
            target_ma_agent_id=AGENT_ID,
            target_name="test-agent",
            reason="task_handoff",
            idempotency_key=idempotency_key,
            requested_work=requested_work,
        )
        await session.commit()

    # Turn 2: another mention in the same thread. The handoff binding is now
    # active (destination unchanged), so this turn runs and completes
    # normally; its completion is what triggers dispatch of the pending
    # continuation, still inside the same `_handle_app_mention` call.
    # `SlackDriver`'s default `conversations.replies` stub already returns an
    # empty history page, so `_latest_human_message_at` naturally sees no
    # superseding message without needing a patch.
    posted_2 = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=parent_channel_id,
        user_id=user_id,
        text="still there?",
        thread_ts=thread_id,
    )

    # Exactly one follow-up turn ran: the agent's reply appears twice in what
    # this single mention call posted (turn 2's own answer, plus the
    # dispatched continuation's answer) -- never a third time.
    assert sum(1 for p in posted_2 if AGENT_TEXT in p) == 2, (
        "turn 2 and the dispatched continuation should each post the agent's reply once"
    )

    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=idempotency_key)
    assert row is not None
    assert row.status == "delivered", "the continuation should settle as delivered, not skipped"

    async with db_session_factory() as session:
        events = await usage_events.list_for_tenant(session, tenant_id=tenant.id)
    # Turn 1 + turn 2 + the dispatched continuation's own turn -- three billed
    # turns, never a fourth (a double-dispatch would bill a fourth here).
    assert len(events) == 3, f"expected exactly 3 billed turns, got {len(events)}"
