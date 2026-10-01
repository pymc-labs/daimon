"""Key removal and replacement on an agent shared only by a routine or a live session.

The scheduler mounts an agent's keys every time a routine fires, and a live
thread session runs with them now, so either one makes the agent shared: a
member — or the agent's own key — may not delete-then-add, remove, or replace
a key there. An agent nobody else uses stays the caller's to manage.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agent_removal import (
    _remove_agent_key_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.credential_requests import (
    _require_key_replacement_allowed,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.self_edit import (
    _self_delete_file_impl,  # pyright: ignore[reportPrivateUsage]
    _self_write_file_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_files import get_agent_file, put_agent_file
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_routine, make_tenant, make_thread_session
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_MA_ID = "ag_routine"
_NAME = "nightly-report"
_ME = "U_ME"


def _agent(tenant_id: uuid.UUID) -> Any:
    return ma_agent(
        id=_MA_ID,
        name=_NAME,
        metadata={"daimon_tenant": str(tenant_id), "daimon_name": _NAME},
    )


def _runtime(db: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> tuple[McpRuntime, Any]:
    agent = _agent(tenant_id)
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([agent.model_dump(mode="json")]))
    client: AsyncAnthropic = build_fake_anthropic(router.dispatch)
    settings = MagicMock()
    settings.mcp.public_url = None
    runtime = McpRuntime(
        session_factory=db,
        client=client,
        settings=settings,  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )
    return runtime, agent


async def _setup(
    db: async_sessionmaker[AsyncSession], *, user: str = "routine", by: str = "U_OTHER"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A tenant whose agent holds GITHUB_TOKEN and is used only by `user`."""
    async with db() as session, session.begin():
        tenant = await make_tenant(session, platform="discord")
        me = await make_account(session, tenant=tenant)
        other = await make_account(session, tenant=tenant)
        if user == "routine":
            await make_routine(
                session, tenant=tenant, created_by_user_id=by, agent_id=_MA_ID, agent_name=_NAME
            )
        elif user == "session":
            await make_thread_session(session, tenant=tenant, account=other, ma_agent_id=_MA_ID)
        elif user == "own-routine":
            await make_routine(
                session, tenant=tenant, created_by_user_id=_ME, agent_id=_MA_ID, agent_name=_NAME
            )
        await put_agent_file(
            session,
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_ID),
            key="GITHUB_TOKEN",
            content="the-value-in-use",
            set_by_account_id=None,
        )
    return tenant.id, me.id, derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=_MA_ID)


def _member(
    tenant_id: uuid.UUID, account_id: uuid.UUID, agent_id: uuid.UUID | None
) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=Role.USER,
        platform="discord",
        platform_user_id=_ME,
        agent_id=agent_id,
        is_admin=False,
    )


async def _value(
    db: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> str | None:
    async with db() as session:
        row = await get_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_id, key="GITHUB_TOKEN"
        )
    return None if row is None else row.content


@pytest.mark.parametrize("user", ["routine", "session"])
async def test_self_delete_then_add_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession], user: str
) -> None:
    tenant_id, me, agent_id = await _setup(db_session_factory, user=user)
    runtime, _agent_obj = _runtime(db_session_factory, tenant_id)
    auth = _member(tenant_id, me, agent_id)
    with pytest.raises(ToolError, match="needs a workspace or server admin"):
        await _self_delete_file_impl(runtime, auth, key="GITHUB_TOKEN")
    with pytest.raises(ToolError, match="would replace GITHUB_TOKEN"):
        await _self_write_file_impl(runtime, auth, key="GH_TOKEN", content="attacker")
    assert await _value(db_session_factory, tenant_id, agent_id) == "the-value-in-use"


@pytest.mark.parametrize("user", ["routine", "session"])
async def test_remove_agent_key_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession], user: str
) -> None:
    tenant_id, me, agent_id = await _setup(db_session_factory, user=user)
    runtime, _agent_obj = _runtime(db_session_factory, tenant_id)
    with pytest.raises(ToolError, match="needs a workspace or server admin"):
        await _remove_agent_key_impl(
            runtime,
            _member(tenant_id, me, None),
            agent_name=_NAME,
            key="GITHUB_TOKEN",
            expected_ma_agent_id=_MA_ID,
        )
    assert await _value(db_session_factory, tenant_id, agent_id) == "the-value-in-use"


@pytest.mark.parametrize("user", ["routine", "session"])
async def test_direct_form_replacement_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession], user: str
) -> None:
    tenant_id, me, _agent_id = await _setup(db_session_factory, user=user)
    runtime, agent = _runtime(db_session_factory, tenant_id)
    with pytest.raises(ToolError, match="needs (an|a server or workspace) admin"):
        await _require_key_replacement_allowed(
            runtime, _member(tenant_id, me, None), ma_agent=agent, key="GITHUB_TOKEN"
        )


@pytest.mark.parametrize("user", ["none", "own-routine"])
async def test_an_agent_nobody_else_uses_stays_the_callers_to_manage(
    db_session_factory: async_sessionmaker[AsyncSession], user: str
) -> None:
    """Allowed control: an unused draft, or one only the caller's own routine runs."""
    tenant_id, me, agent_id = await _setup(db_session_factory, user=user)
    runtime, agent = _runtime(db_session_factory, tenant_id)
    await _require_key_replacement_allowed(
        runtime, _member(tenant_id, me, None), ma_agent=agent, key="GITHUB_TOKEN"
    )
    result = await _remove_agent_key_impl(
        runtime,
        _member(tenant_id, me, None),
        agent_name=_NAME,
        key="GITHUB_TOKEN",
        expected_ma_agent_id=_MA_ID,
    )
    assert result.removed is True
    assert await _value(db_session_factory, tenant_id, agent_id) is None
