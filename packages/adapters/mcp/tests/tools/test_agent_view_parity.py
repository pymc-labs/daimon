"""The web read model and the get_agent tool agree on what each caller may read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.agents import _get_agent_impl  # pyright: ignore[reportPrivateUsage]
from daimon.core import agent_view
from daimon.core.agent_view import AgentViewDeps, AgentViewer
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_owners
from daimon.core.stores.domain import Role
from daimon.core.stores.turn_origins import create_origin
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SERVER: dict[str, Any] = {"name": "docs", "type": "url", "url": "https://docs.example.com/mcp"}


@pytest.mark.parametrize("who", ["admin", "owner", "member"])
async def test_view_and_get_agent_show_the_same_prompt_and_capabilities(
    who: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    owner = await make_account(db_session, tenant=tenant)
    caller = owner if who == "owner" else await make_account(db_session, tenant=tenant)
    await agent_owners.record_creator(
        db_session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_p"),
        account_id=owner.id,
    )
    now = datetime.now(UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=caller.id,
        platform=tenant.platform,
        parent_channel_id="C1",
        thread_id="1.2",
        responder_ma_agent_id="ag_p",
        responder_name="helper",
        configuration_target_ma_agent_id="ag_p",
        configuration_target_name="helper",
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        now=now,
        expires_at=now + timedelta(minutes=10),
    )
    await db_session.commit()
    agent = ma_agent(
        id="ag_p",
        name="helper",
        tenant_id=tenant.id,
        metadata={"daimon_account": str(owner.id)},
        system="Be brief.",
        description="Answers questions",
        mcp_servers=[_SERVER],
    )
    router = MARouter()
    router.add("GET", r"/v1/agents$", lambda _r, _m: list_response([agent.model_dump(mode="json")]))
    router.add_agent(agent)
    client = build_fake_anthropic(router.dispatch)
    settings = MagicMock()
    settings.mcp.public_url = None
    runtime = McpRuntime(
        session_factory=db_session_factory,
        client=client,
        settings=settings,
        deployment_default=DeploymentDefault(agent_name="helper"),
    )
    admin = who == "admin"
    auth = AuthIdentity(
        account_id=caller.id,
        tenant_id=tenant.id,
        role=Role.ADMIN if admin else Role.USER,
        platform=tenant.platform,
        platform_user_id=str(caller.id),
        chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_p"),
        is_admin=admin,
    )

    info = await _get_agent_impl(
        runtime, auth, "helper", expected_ma_agent_id="ag_p", origin_context_id=str(origin.id)
    )
    view = await agent_view.load_agent_view(
        AgentViewDeps(client=client, session_factory=db_session_factory, markup=Decimal(1)),
        tenant_id=tenant.id,
        ma_agent_id="ag_p",
        viewer=AgentViewer(account_id=caller.id, is_admin=admin, may_edit=True),
        month_start=datetime(2026, 10, 1, tzinfo=UTC),
    )

    assert view is not None, "the agent loads"
    assert view.system == info.system, f"{who}: both show or withhold the same prompt"
    assert view.purpose == (info.description or ""), f"{who}: the same purpose"
    assert view.model.id == info.model, f"{who}: the same model"
    assert [s.skill_id for s in view.skills] == [s.skill_id for s in info.skills], (
        f"{who}: the same skills"
    )
    urls = {s.url for s in info.mcp_servers or []}
    assert {s.url for s in view.mcp_servers} - {""} == urls, (
        f"{who}: server URLs are shown to the same callers"
    )
