"""Who may call Daimon's own `web_fetch` (`check_web_fetch_allowed`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.host_web_fetch import check_web_fetch_allowed
from daimon.core.authz import Subject
from daimon.core.config import DatabaseSettings, Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role, SandboxPin
from daimon.core.stores.routines import set_sandbox_tools
from daimon.core.stores.turn_origins import create_origin
from daimon.testing.factories import make_account, make_routine, make_tenant
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=cast(AsyncAnthropic, object()),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            _env_file=None,  # type: ignore[call-arg]
        ),
        deployment_default=DeploymentDefault(),
    )


def _auth(tenant_id: uuid.UUID, **extra: object) -> AuthIdentity:
    return AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER, **extra)  # pyright: ignore[reportArgumentType]


async def test_web_fetch_when_not_a_provider_turn_then_refused(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ToolError, match="not available"):
        await check_web_fetch_allowed(_runtime(sessionmaker), _auth(uuid.uuid4()), "ctx")


async def test_web_fetch_when_conversation_has_no_origin_then_refused(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    auth = _auth(uuid.uuid4(), no_confirmation_surface=True)
    with pytest.raises(ToolError, match="needs a conversation"):
        await check_web_fetch_allowed(_runtime(sessionmaker), auth, None)


@pytest.mark.parametrize("opted_in", [False, True])
async def test_web_fetch_when_routine_then_follows_its_built_in_tools(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], opted_in: bool
) -> None:
    tenant = await make_tenant(db_session)
    routine = await make_routine(db_session, tenant=tenant)
    if opted_in:
        pin = SandboxPin(
            agent_id=routine.agent_id,
            agent_version=1,
            environment_id="env_1",
            environment_updated_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
        await set_sandbox_tools(
            db_session,
            routine.id,
            tenant_id=tenant.id,
            enabled=True,
            pin=pin,
            set_by="admin-1",
            actor=Subject(is_admin=True, platform_user_id="admin-1"),
            via="mcp",
        )
    await db_session.commit()
    auth = _auth(
        tenant.id, no_confirmation_surface=True, routine_id=routine.id, execution_id=uuid.uuid4()
    )
    if opted_in:
        await check_web_fetch_allowed(_runtime(sessionmaker), auth, None)
    else:
        with pytest.raises(ToolError, match="off for this routine"):
            await check_web_fetch_allowed(_runtime(sessionmaker), auth, None)


@pytest.mark.parametrize("leave", [False, True])
async def test_web_fetch_when_nobody_attends_then_needs_the_turns_signed_leave(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession], leave: bool
) -> None:
    """Unattended, host web_fetch follows the entry point's built-in policy (A-M2)."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        parent_channel_id="channel-1",
        thread_id="thread-1",
        responder_ma_agent_id="agent_x",
        responder_name="Researcher",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        expires_at=now + timedelta(hours=1),
        now=now,
        sender_foreign=False,
        shared_channel=False,
        attended=False,
        chat_session_id="chat-1",
    )
    await db_session.commit()
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        chat_session_id="chat-1",
        no_confirmation_surface=True,
        host_builtins=leave,
    )
    if leave:
        try:
            await check_web_fetch_allowed(_runtime(sessionmaker), auth, str(origin.id))
        except ToolError as exc:  # The place's own switch may still be off.
            assert "nobody" not in str(exc), f"signed leave must pass the gate: {exc}"
    else:
        with pytest.raises(ToolError, match="nobody is in the conversation"):
            await check_web_fetch_allowed(_runtime(sessionmaker), auth, str(origin.id))
