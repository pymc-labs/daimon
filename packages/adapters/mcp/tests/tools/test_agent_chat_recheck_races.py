"""A re-check must read policy after its agent-name lookup finishes."""

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.middleware.mcp_identity import (
    IdentityMiddleware,
    production_agent_id_resolver,
    production_internal_resolver,
    production_is_admin_resolver,
    production_role_resolver,
    production_subject_resolver,
    production_tenant_resolver,
)
from daimon.adapters.mcp.tools.agent_chat import register_agent_chat_tools
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.access_policy import set_access_policy
from daimon.testing import AGENT_ID, build_turn_router
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import session_response
from daimon.testing.ma_models import DEFAULT_AGENT_NAME
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.transforms import Visibility

from .test_agent_chat import _runtime


@pytest.mark.parametrize("tool", ["continue_turn", "ask"])
async def test_pin_changed_during_recheck_agent_lookup_prevents_send(
    db_session, db_session_factory, tool
):
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"unrelated-agent": ("elsewhere",)}),
    )
    await db_session.commit()
    sent = []
    router = build_turn_router(
        str(tenant.id),
        sent_event_bodies=sent,
        send_events_data=[
            {
                "id": "sevt_review",
                "type": "user.message",
                "content": [{"type": "text", "text": "hello"}],
                "processed_at": "2026-10-01T12:00:00Z",
            }
        ],
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)$",
        lambda _r, m: session_response(
            session_id=m.group(1),
            status=("terminated" if sent and tool == "ask" else "idle"),
            agent_id=AGENT_ID,
            metadata={"daimon_account": str(account.id)},
        ),
    )
    lookups = 0
    changed = False

    async def changing(req):
        nonlocal lookups, changed
        if req.method == "GET" and req.url.path == "/v1/agents":
            lookups += 1
            if lookups == 2:
                async with db_session_factory.begin() as session:
                    await set_access_policy(
                        session,
                        tenant_id=tenant.id,
                        policy=TenantAccessPolicy(
                            agent_channel_pins={DEFAULT_AGENT_NAME: ("elsewhere",)}
                        ),
                    )
                changed = True
        return router.dispatch(req)

    client = AsyncAnthropic(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(changing))
    )
    token = "sol-review-token"
    claims = {
        "sub": str(account.id),
        "tenant_id": str(tenant.id),
        "role": "user",
        "agent_id": str(derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=AGENT_ID)),
        "client_id": "test",
    }
    mcp = FastMCP(name="sol-review", auth=StaticTokenVerifier(tokens={token: claims}))
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=production_subject_resolver,
            tenant_resolver=production_tenant_resolver,
            role_resolver=production_role_resolver,
            agent_id_resolver=production_agent_id_resolver,
            is_admin_resolver=production_is_admin_resolver,
            internal_resolver=production_internal_resolver,
            sessionmaker=db_session_factory,
        )
    )
    mcp.add_transform(Visibility(False, tags={"agent-chat"}))
    register_agent_chat_tools(
        mcp, _runtime(client, session_factory=db_session_factory), billing_config=None
    )
    result = await call_mcp_tool(
        mcp.http_app(),
        token=token,
        name=tool,
        arguments={"handle": "ses_existing", "message": "hello"},
    )
    assert changed, (lookups, result)
    assert sent == [], f"user event sent after pin changed: count={len(sent)}, result={result}"
    assert result["result"].get("isError"), result
