"""Regression: a pin landing during an await inside the OAuth vault and attach helpers refuses the write."""

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_platform_principal

from .test_oauth_mcp import _app, _fake_ma, _notion, _seed_flow


@pytest.mark.parametrize("window", ["credential-list", "attach-agent-fetch"])
async def test_oauth_pin_during_inner_helper_await(db_session, db_session_factory, window):
    flow, tenant_id = await _seed_flow(db_session, with_origin=True)
    tenant = await get_tenant(db_session, tenant_id)
    account = await get_account(db_session, flow.account_id)
    assert tenant is not None and account is not None
    await make_platform_principal(
        db_session, platform="discord", external_id="requester-1", tenant=tenant, account=account
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant_id,
        policy=TenantAccessPolicy(agent_channel_pins={"daimon": ("chan-1",)}),
    )
    await set_channel_admins(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        channel_id="chan-1",
        role_ids=[],
        user_ids=["requester-1"],
        actor_account_id=None,
    )
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant_id, channel_id="chan-1"),
        tenant_id=tenant_id,
        agent_name="daimon",
        mode="agent",
    )
    await db_session.commit()
    fake, created, updates = _fake_ma(tenant_id, account_id=flow.account_id, agent_id=flow.agent_id)
    inner = fake._client._transport
    credential_lists = 0
    changed = False
    deletes = []

    async def changing(req):
        nonlocal credential_lists, changed
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_me/credentials":
            credential_lists += 1
        target = (
            window == "credential-list"
            and credential_lists == 2
            and req.method == "GET"
            and req.url.path == "/v1/vaults/vlt_me/credentials"
        ) or (
            window == "attach-agent-fetch"
            and req.method == "GET"
            and req.url.path == "/v1/agents/ag_oauth"
        )
        if target and not changed:
            changed = True
            async with db_session_factory.begin() as session:
                await set_access_policy(
                    session,
                    tenant_id=tenant_id,
                    policy=TenantAccessPolicy(agent_channel_pins={"daimon": ("C_PINNED",)}),
                )
        if req.method == "DELETE" and req.url.path.endswith("/vcrd_oauth"):
            deletes.append(req.url.path)
            return httpx.Response(204)
        return await inner.handle_async_request(req)

    anthropic = AsyncAnthropic(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(changing))
    )
    app = _app(
        db_session_factory,
        anthropic=anthropic,
        transport=_notion([]),
        deployment_default=DeploymentDefault(agent_name="other"),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/oauth/mcp/start?state={flow.state}")
        done = await client.get(f"/oauth/mcp/callback?code=code123&state={flow.state}")
    assert changed, (window, credential_lists)
    if window == "credential-list":
        assert created == [], (
            f"grant saved after pin changed; status={done.status_code}, grants={len(created)}, updates={len(updates)}, cleanup={deletes}"
        )
    assert updates == [], (
        f"agent attached after pin changed; status={done.status_code}, grants={len(created)}, updates={len(updates)}, cleanup={deletes}"
    )
    assert done.status_code == 403
