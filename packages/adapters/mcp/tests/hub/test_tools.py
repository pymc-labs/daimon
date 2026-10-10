"""Hub tools address daimons by derived UUID and act as the caller's account in that tenant."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.hub.identity import HubIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._session_access import load_hub_subject
from daimon.adapters.mcp.tools.hub import (  # pyright: ignore[reportPrivateUsage]
    _auth_for,
    _list_daimons_impl,
    _resolve_daimon,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.defaults.loader import DeploymentDefault
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ChannelScopeRef, TenantScopeRef
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_platform_role_ids, set_role
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing import ma_agent
from daimon.testing.factories import make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _runtime(client: AsyncAnthropic, session_factory: Any) -> McpRuntime:
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.github.fallback_pat = None
    return McpRuntime(
        session_factory=session_factory,
        client=client,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(environment_name="production"),
    )


def _agents_router(
    agents_by_tenant: dict[uuid.UUID, list[tuple[str, str]]],
    listings: list[str] | None = None,
) -> MARouter:
    """``listings`` collects one entry per GET /v1/agents page request when given."""
    router = MARouter()
    payload = [
        ma_agent(
            id=ma_id,
            name=name,
            system="Answers ops questions",
            metadata={"daimon_tenant": str(tid), "daimon_name": name},
        ).model_dump(mode="json")
        for tid, agents in agents_by_tenant.items()
        for ma_id, name in agents
    ]

    def _list(_r: Any, _m: Any) -> Any:
        if listings is not None:
            listings.append("agents.list")
        return list_response(payload)

    router.add("GET", r"/v1/agents", _list)
    return router


async def _two_tenant_hub(db_session: AsyncSession) -> HubIdentity:
    t1 = await make_tenant(db_session, platform="discord", workspace_id="g1")
    t2 = await make_tenant(db_session, platform="discord", workspace_id="g2")
    p1 = await make_platform_principal(db_session, platform="discord", external_id="u1", tenant=t1)
    p2 = await make_platform_principal(db_session, platform="discord", external_id="u1", tenant=t2)
    for tenant in (t1, t2):
        await set_fields(
            db_session,
            tenant_id=tenant.id,
            scope=TenantScopeRef(tenant_id=tenant.id),
            agent_name="helper",
        )
    await db_session.commit()
    return HubIdentity(
        platform="discord",
        platform_user_id="u1",
        tenants=(
            HubTenant(
                tenant_id=t1.id, account_id=p1.account_id, workspace_id="g1", workspace_name="PyMC"
            ),
            HubTenant(
                tenant_id=t2.id, account_id=p2.account_id, workspace_id="g2", workspace_name="Bayes"
            ),
        ),
    )


async def test_list_daimons_spans_every_tenant_and_disambiguates_same_names(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    hub = await _two_tenant_hub(db_session)
    t1, t2 = hub.tenants
    client = build_fake_anthropic(
        _agents_router(
            {t1.tenant_id: [("ag_1", "helper")], t2.tenant_id: [("ag_2", "helper")]}
        ).dispatch
    )

    daimons = await _list_daimons_impl(_runtime(client, db_session_factory), hub)

    assert [(d.name, d.workspace) for d in daimons] == [("helper", "Bayes"), ("helper", "PyMC")], (
        f"got {daimons!r}"
    )
    assert {d.id for d in daimons} == {
        str(derive_agent_uuid(tenant_id=t1.tenant_id, ma_agent_id="ag_1")),
        str(derive_agent_uuid(tenant_id=t2.tenant_id, ma_agent_id="ag_2")),
    }, f"ids must be the derived per-agent UUIDs, got {[d.id for d in daimons]!r}"
    assert daimons[0].platform == "discord" and daimons[0].role_summary.startswith("Answers ops")


async def test_list_daimons_leaves_out_only_a_tenant_whose_policy_cant_be_read(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    hub = await _two_tenant_hub(db_session)
    t1, t2 = hub.tenants
    await set_fields(
        db_session,
        tenant_id=t2.tenant_id,
        scope=TenantScopeRef(tenant_id=t2.tenant_id),
        agent_name="b",
    )
    await db_session.execute(
        text("INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, 'null'::jsonb)"),
        {"t": t1.tenant_id},
    )
    await db_session.commit()
    client = build_fake_anthropic(
        _agents_router({t1.tenant_id: [("ag_1", "a")], t2.tenant_id: [("ag_2", "b")]}).dispatch
    )

    daimons = await _list_daimons_impl(_runtime(client, db_session_factory), hub)

    assert [(d.name, d.workspace) for d in daimons] == [("b", "Bayes")], (
        "the unreadable tenant is skipped, not every tenant"
    )
    with pytest.raises(ToolError, match="not found"):
        await _resolve_daimon(
            _runtime(client, db_session_factory),
            hub,
            str(derive_agent_uuid(tenant_id=t1.tenant_id, ma_agent_id="ag_1")),
        )


async def test_hub_never_lists_or_resolves_an_isolated_channels_own_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    hub = await _two_tenant_hub(db_session)
    t1 = hub.tenants[0]
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=t1.tenant_id, channel_id="room"),
        tenant_id=t1.tenant_id,
        agent_name="local",
        mode="agent",
    )
    await set_access_policy(
        db_session,
        tenant_id=t1.tenant_id,
        policy=TenantAccessPolicy(
            sealed_channel_ids=("room",),
            isolated_channel_ids=("room",),
            agent_channel_pins={"local": ("room",)},
        ),
    )
    await db_session.commit()
    router = _agents_router({t1.tenant_id: [("ag_1", "helper"), ("ag_2", "local")]})
    runtime = _runtime(build_fake_anthropic(router.dispatch), db_session_factory)

    daimons = await _list_daimons_impl(runtime, hub)
    assert [d.name for d in daimons] == ["helper"], "the isolated channel's agent stays hidden"
    hidden = str(derive_agent_uuid(tenant_id=t1.tenant_id, ma_agent_id="ag_2"))
    with pytest.raises(ToolError, match="not found"):
        await _resolve_daimon(runtime, hub, hidden)


async def test_hub_looks_up_a_stored_slack_group_with_no_session_open(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A slow Slack must not hold a pooled connection while the hub lists or reads."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T0HUB")
    principal = await make_platform_principal(
        db_session, platform="slack", external_id="U1", tenant=tenant
    )
    await set_platform_role_ids(db_session, principal.account_id, ["S1"])
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        channel_id="C0ROOM",
        role_ids=["S1"],
        user_ids=[],
        actor_account_id=None,
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            sealed_channel_ids=("C0ROOM",),
            isolated_channel_ids=("C0ROOM",),
            agent_channel_pins={"local": ("C0ROOM",)},
        ),
    )
    await db_session.commit()
    open_sessions = [0]
    seen: list[int] = []

    @asynccontextmanager
    async def counting() -> AsyncIterator[AsyncSession]:
        open_sessions[0] += 1
        try:
            async with db_session_factory() as session:
                yield session
        finally:
            open_sessions[0] -= 1

    async def members(group_id: str) -> frozenset[str]:
        seen.append(open_sessions[0])
        return frozenset({"U1"})

    lookups = MagicMock()
    lookups.members.return_value = members
    router = _agents_router({tenant.id: [("ag_1", "local")]})
    runtime = replace(
        _runtime(build_fake_anthropic(router.dispatch), counting), group_lookups=lookups
    )
    hub = HubIdentity(
        platform="slack",
        platform_user_id="U1",
        tenants=(
            HubTenant(
                tenant_id=tenant.id,
                account_id=principal.account_id,
                workspace_id="T0HUB",
                workspace_name="Acme",
            ),
        ),
    )
    auth = AuthIdentity(
        account_id=principal.account_id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="slack",
        external_id="T0HUB",
        platform_user_id="U1",
    )

    daimons = await _list_daimons_impl(runtime, hub)
    subject = await load_hub_subject(runtime, auth)

    assert [d.name for d in daimons] == ["local"], "the live group admin sees the channel's agent"
    assert subject.administered_channel_ids == frozenset({"C0ROOM"}), "and administers it"
    assert seen == [0, 0], "each lookup ran after its session closed"


async def test_list_daimons_pages_the_org_once_however_many_tenants_the_caller_has(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The org agent listing is the expensive call; a login spanning several workspaces
    must not pay for it once per workspace."""
    hub = await _two_tenant_hub(db_session)
    await set_fields(
        db_session,
        tenant_id=hub.tenants[1].tenant_id,
        scope=TenantScopeRef(tenant_id=hub.tenants[1].tenant_id),
        agent_name="ops",
    )
    await db_session.commit()
    listings: list[str] = []
    router = _agents_router(
        {
            hub.tenants[0].tenant_id: [("ag_1", "helper")],
            hub.tenants[1].tenant_id: [("ag_2", "ops")],
        },
        listings,
    )
    runtime = _runtime(build_fake_anthropic(router.dispatch), db_session_factory)

    daimons = await _list_daimons_impl(runtime, hub)

    assert len(daimons) == 2, f"got {daimons!r}"
    assert listings == ["agents.list"], (
        f"expected one org listing for two tenants, got {len(listings)}"
    )


async def test_resolve_daimon_rejects_ids_outside_the_callers_tenants(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    hub = await _two_tenant_hub(db_session)
    t1, _ = hub.tenants
    client = build_fake_anthropic(_agents_router({t1.tenant_id: [("ag_1", "helper")]}).dispatch)
    runtime = _runtime(client, db_session_factory)

    foreign = derive_agent_uuid(tenant_id=uuid.uuid4(), ma_agent_id="ag_1")
    with pytest.raises(ToolError, match="daimon not found"):
        await _resolve_daimon(runtime, hub, str(foreign))
    with pytest.raises(ToolError, match="daimon not found"):
        await _resolve_daimon(runtime, hub, "not-a-uuid")


async def test_auth_for_acts_as_the_callers_account_in_that_tenant(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    hub = await _two_tenant_hub(db_session)
    t1, _ = hub.tenants
    client = build_fake_anthropic(_agents_router({t1.tenant_id: [("ag_1", "helper")]}).dispatch)
    runtime = _runtime(client, db_session_factory)

    tenant, agent = await _resolve_daimon(
        runtime, hub, str(derive_agent_uuid(tenant_id=t1.tenant_id, ma_agent_id="ag_1"))
    )
    auth = await _auth_for(runtime, hub, tenant, agent)

    assert auth.account_id == t1.account_id and auth.tenant_id == t1.tenant_id, f"got {auth!r}"
    assert auth.agent_id == derive_agent_uuid(tenant_id=t1.tenant_id, ma_agent_id="ag_1")
    assert auth.platform == "discord" and auth.platform_user_id == "u1" and auth.external_id == "g1"
    assert auth.role is Role.USER and auth.is_admin is False, "hub identities never carry admin"


@pytest.mark.parametrize("admin", [False, True])
async def test_hub_member_lists_only_workspace_responder_while_admin_sees_drafts(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    admin: bool,
) -> None:
    hub = await _two_tenant_hub(db_session)
    tenant = hub.tenants[0]
    await set_role(db_session, tenant.account_id, Role.ADMIN if admin else Role.USER)
    await db_session.commit()
    client = build_fake_anthropic(
        _agents_router(
            {
                tenant.tenant_id: [("ag_helper", "helper"), ("ag_draft", "draft")],
            }
        ).dispatch
    )
    runtime = _runtime(client, db_session_factory)
    listed = await _list_daimons_impl(runtime, hub)
    assert {agent.name for agent in listed} == ({"helper", "draft"} if admin else {"helper"})
    hidden = str(derive_agent_uuid(tenant_id=tenant.tenant_id, ma_agent_id="ag_draft"))
    if not admin:
        with pytest.raises(ToolError, match="not found"):
            await _resolve_daimon(runtime, hub, hidden)
