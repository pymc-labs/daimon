"""The timer tools through real stores: create in a turn, list, cancel."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.timers import (
    _cancel_timer_impl,  # pyright: ignore[reportPrivateUsage]
    _create_timer_impl,  # pyright: ignore[reportPrivateUsage]
    _list_timers_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.continuity.wakes import claim_wake
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.turn_origin import turn_origin
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_stub_anthropic
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=build_stub_anthropic(lambda _r: httpx.Response(404)),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
            anthropic=AnthropicSettings(api_key=SecretStr("test")),
        ),
        deployment_default=DeploymentDefault(),
    )


def _auth(tenant_id: uuid.UUID, account_id: uuid.UUID, *, role: Role = Role.USER) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=role,
        platform="discord",
        platform_user_id="42",
    )


async def test_create_list_and_cancel_a_timer_from_a_turn(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker)
    auth = _auth(tenant.id, caller.id)
    fire_at = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0)

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id="agt_daimon",
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        timer = await _create_timer_impl(
            runtime,
            auth,
            origin_context_id=str(origin.id),
            fire_at=fire_at.astimezone().isoformat(),
            note="  see if the deploy finished and tell Carla  ",
        )

    assert timer.fire_at == fire_at and timer.thread_id == "T_THREAD"
    assert (
        timer.agent_name == "daimon" and timer.note == "see if the deploy finished and tell Carla"
    )
    async with committing_sessionmaker() as session:
        row = await get_continuation(session, idempotency_key=uuid.UUID(timer.timer_id))
    assert row is not None and row.reason == "timer" and row.status == "pending"
    assert row.target_ma_agent_id == "agt_daimon", "the timer resumes the agent that set it"
    assert row.requester_external_user_id == "42", "the timer runs as the person who asked"

    assert [t.timer_id for t in await _list_timers_impl(runtime, auth)] == [timer.timer_id]
    assert await _list_timers_impl(runtime, _auth(tenant.id, other.id)) == []

    with pytest.raises(ToolError, match="timer not found"):
        await _cancel_timer_impl(runtime, _auth(tenant.id, other.id), timer_id=timer.timer_id)
    result = await _cancel_timer_impl(runtime, auth, timer_id=timer.timer_id)
    assert result.cancelled is True
    assert (
        await claim_wake(
            committing_sessionmaker,
            idempotency_key=uuid.UUID(timer.timer_id),
            now=fire_at + timedelta(minutes=1),
        )
        is None
    ), "a cancelled timer never fires"
    with pytest.raises(ToolError, match="timer not found"):
        await _cancel_timer_impl(runtime, auth, timer_id=timer.timer_id)


async def test_create_timer_refuses_a_time_without_an_offset(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    runtime = _runtime(committing_sessionmaker)

    async with turn_origin(
        committing_sessionmaker,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform="discord",
        parent_channel_id="C_PARENT",
        thread_id="T_THREAD",
        responder_ma_agent_id="agt_daimon",
        responder_name="daimon",
        role=Role.USER,
    ) as origin:
        with pytest.raises(ToolError, match="UTC offset"):
            await _create_timer_impl(
                runtime,
                _auth(tenant.id, caller.id),
                origin_context_id=str(origin.id),
                fire_at="2099-01-01T09:00:00",
                note="check the build",
            )


async def test_create_timer_needs_an_active_turn_origin(
    db_session: AsyncSession,
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    with pytest.raises(ToolError, match="origin"):
        await _create_timer_impl(
            _runtime(committing_sessionmaker),
            _auth(tenant.id, caller.id),
            origin_context_id=str(uuid.uuid4()),
            fire_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            note="check the build",
        )
