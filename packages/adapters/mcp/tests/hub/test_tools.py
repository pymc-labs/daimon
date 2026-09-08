"""Hub tools address daimons by derived UUID and act as the caller's account in that tenant."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.hub.identity import HubIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.hub import (  # pyright: ignore[reportPrivateUsage]
    _auth_for,
    _list_daimons_impl,
    _resolve_daimon,
)
from daimon.core.defaults.loader import DeploymentDefault
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.domain import Role
from daimon.testing.factories import make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from factories import make_ma_agent
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


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
        make_ma_agent(
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


async def test_list_daimons_pages_the_org_once_however_many_tenants_the_caller_has(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The org agent listing is the expensive call; a login spanning several workspaces
    must not pay for it once per workspace."""
    hub = await _two_tenant_hub(db_session)
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
