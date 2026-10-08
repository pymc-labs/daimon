"""Regression: a pin added during the OAuth vault lookup stores no grant and attaches nothing."""

import httpx
from anthropic import AsyncAnthropic
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.testing.ma import sdk_http_client

from .test_oauth_mcp import _app, _fake_ma, _notion, _seed_flow


async def test_a_pin_added_during_vault_lookup_stores_no_grant(db_session, db_session_factory):
    flow, tenant_id = await _seed_flow(db_session)
    await db_session.commit()
    fake, created, updates = _fake_ma(tenant_id, account_id=flow.account_id, agent_id=flow.agent_id)
    inner = fake._client._transport

    async def pin_during_vault_lookup(req):
        if req.method == "GET" and req.url.path == "/v1/vaults":
            async with db_session_factory.begin() as session:
                await set_access_policy(
                    session,
                    tenant_id=tenant_id,
                    policy=TenantAccessPolicy(agent_channel_pins={"daimon": ("C_PINNED",)}),
                )
        return await inner.handle_async_request(req)

    anthropic = AsyncAnthropic(
        api_key="test",
        http_client=sdk_http_client(
            httpx.AsyncClient(transport=httpx.MockTransport(pin_during_vault_lookup))
        ),
    )
    app = _app(db_session_factory, anthropic=anthropic, transport=_notion([]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/oauth/mcp/start?state={flow.state}")
        done = await client.get(f"/oauth/mcp/callback?code=code123&state={flow.state}")
    assert created == [], f"grant saved after pin changed: {created}; response={done.status_code}"
    assert updates == [], "agent configured after pin changed"
    assert done.status_code == 403
