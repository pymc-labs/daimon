"""A hub turn on a pinned agent is refused: an MCP turn runs in no channel."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.testing import AGENT_ID, build_turn_router
from daimon.testing.factories import make_ledger_entry, make_platform_principal, make_tenant
from daimon.testing.ma import build_fake_anthropic
from daimon.testing.ma_models import DEFAULT_AGENT_NAME
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_turn_parity import _TOKEN, _call_tool, _runtime  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("tool_name", "handle"),
    [("ask", None), ("ask", "existing"), ("start_turn", None), ("continue_turn", "existing")],
    ids=["ask-new", "ask-resume", "start_turn", "continue_turn"],
)
@pytest.mark.parametrize("role", [Role.USER, Role.ADMIN])
async def test_hub_refuses_a_members_turn_and_exempts_an_admins_on_a_pinned_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tool_name: str,
    handle: str | None,
    role: Role,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="u1", tenant=tenant
    )
    await set_role(db_session, principal.account_id, role)
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={DEFAULT_AGENT_NAME: ("c-acme",)}),
    )
    await db_session.commit()

    hub_tenant = HubTenant(
        tenant_id=tenant.id,
        account_id=principal.account_id,
        workspace_id="g1",
        workspace_name="PyMC",
    )
    claims = encode_hub_claims(platform="discord", platform_user_id="u1", tenants=[hub_tenant])
    auth = StaticTokenVerifier(
        tokens={_TOKEN: {"sub": "u1", "client_id": "c", "upstream_claims": claims}}
    )
    router = build_turn_router(str(tenant.id))
    runtime = _runtime(build_fake_anthropic(router.dispatch), db_session_factory)
    mcp = build_hub_app(platform="discord", runtime=runtime, auth=auth, billing_config=None)
    daimon_id = str(derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=AGENT_ID))

    create_session = AsyncMock(side_effect=AssertionError("a pinned agent must not start"))
    with patch("daimon.adapters.mcp.tools.agent_chat.create_session", new=create_session):
        result = await _call_tool(
            mcp.http_app(path="/mcp", stateless_http=True, json_response=True),
            "/mcp",
            _TOKEN,
            name=tool_name,
            arguments={
                "daimon_id": daimon_id,
                "message": "summarize your memory",
                **({"handle": handle} if handle is not None else {}),
            },
        )

    payload = result["result"]
    if role is Role.ADMIN:
        # An admin's hub turn reaches only them, so the pin doesn't apply.
        assert "pinned this agent" not in str(payload)
        return
    assert payload["isError"], f"a pinned agent must be refused over the hub, got {payload!r}"
    assert "pinned this agent" in str(payload)
    create_session.assert_not_awaited()
