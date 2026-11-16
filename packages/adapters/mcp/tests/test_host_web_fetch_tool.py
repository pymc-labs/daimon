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
from daimon.core.config import DatabaseSettings, Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import FeatureTarget, Role, SandboxPin
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.stores.sandbox_pins import SANDBOX_KEY, put_sandbox_pin
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


@pytest.mark.parametrize(
    ("opted_in", "matched", "allowed"),
    [(False, False, False), (True, False, False), (True, True, True)],
    ids=["no_opt_in", "pin_row_but_environment_mismatch", "pin_matched"],
)
async def test_web_fetch_when_routine_then_follows_the_runs_own_match(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    opted_in: bool,
    matched: bool,
    allowed: bool,
) -> None:
    """A pin row alone never opens it; only the run's signed match does."""
    tenant = await make_tenant(db_session)
    routine = await make_routine(db_session, tenant=tenant)
    if opted_in:
        pin = SandboxPin(
            agent_id=routine.agent_id,
            agent_version=1,
            environment_id="env_1",
            environment_updated_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
        key = str(routine.id)
        await set_feature_value(
            db_session, tenant_id=tenant.id, key=SANDBOX_KEY, value="on", set_by=None,
            target=FeatureTarget("routine", routine_id=key), surface="chat",
        )  # fmt: skip
        await put_sandbox_pin(db_session, tenant_id=tenant.id, tier="routine", key=key, pin=pin)
    await db_session.commit()
    auth = _auth(
        tenant.id,
        no_confirmation_surface=True,
        routine_id=routine.id,
        execution_id=uuid.uuid4(),
        host_builtins=matched,
    )
    if allowed:
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
