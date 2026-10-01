"""Regression: a change saved while a hub turn is under way stops it before create and send.

`_admit` decides when the tool call starts; the session create and the
message send come after MA round trips. A pin (or an admin's demotion) saved
during those round trips must stop the turn itself, not only the next one.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions.beta_managed_agents_text_block import BetaManagedAgentsTextBlock
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event import (
    BetaManagedAgentsUserMessageEvent,
)
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.testing import AGENT_ID, build_turn_router, ma_session
from daimon.testing.factories import make_ledger_entry, make_platform_principal, make_tenant
from daimon.testing.ma import session_response
from daimon.testing.ma_models import DEFAULT_AGENT_NAME
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from .test_turn_parity import _TOKEN, _call_tool, _runtime

_PINNED = TenantAccessPolicy(agent_channel_pins={DEFAULT_AGENT_NAME: ("elsewhere",)})


async def _hub_call(
    db_session,
    db_session_factory,
    *,
    tool_name: str,
    change_on: tuple[str, str],
    admin: bool = False,
) -> tuple[dict[str, Any], AsyncMock, list[dict[str, Any]]]:
    """Run one hub turn; the policy pin (and a demotion, for an admin) lands on ``change_on``."""
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="u1", tenant=tenant
    )
    if admin:
        await set_role(db_session, principal.account_id, Role.ADMIN)
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
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
    sent: list[dict[str, Any]] = []
    router = build_turn_router(
        str(tenant.id),
        send_events_data=[
            BetaManagedAgentsUserMessageEvent(
                id="sevt_review",
                content=[BetaManagedAgentsTextBlock(type="text", text="hello")],
                type="user.message",
                processed_at=datetime(2026, 10, 1, tzinfo=UTC),
            ).model_dump(mode="json")
        ],
        sent_event_bodies=sent,
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)$",
        lambda _r, m: session_response(
            session_id=m.group(1),
            status="idle",
            agent_id=AGENT_ID,
            metadata={"daimon_account": str(principal.account_id)},
        ),
    )

    async def changing(req: httpx.Request) -> httpx.Response:
        if (req.method, req.url.path) == change_on:
            async with db_session_factory.begin() as session:
                await set_access_policy(session, tenant_id=tenant.id, policy=_PINNED)
                if admin:
                    await set_role(session, principal.account_id, Role.USER)
        return router.dispatch(req)

    client = AsyncAnthropic(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(changing))
    )
    runtime = _runtime(client, db_session_factory)
    mcp = build_hub_app(platform="discord", runtime=runtime, auth=auth, billing_config=None)
    create = AsyncMock(
        return_value=ma_session(id="ses_review", agent_id=AGENT_ID, status="running")
    )
    with patch("daimon.adapters.mcp.tools.agent_chat.create_session", new=create):
        result = await _call_tool(
            mcp.http_app(path="/mcp", stateless_http=True, json_response=True),
            "/mcp",
            _TOKEN,
            name=tool_name,
            arguments={
                "daimon_id": str(derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=AGENT_ID)),
                "message": "hello",
                **({"handle": "ses_existing"} if tool_name == "continue_turn" else {}),
            },
        )
    return result, create, sent


async def test_member_hub_pin_added_during_environment_lookup_prevents_create_and_send(
    db_session, db_session_factory
):
    result, create, sent = await _hub_call(
        db_session,
        db_session_factory,
        tool_name="start_turn",
        change_on=("GET", "/v1/environments"),
    )
    assert result["result"].get("isError"), result
    create.assert_not_awaited()
    assert sent == []


async def test_hub_continue_on_a_resumed_handle_rechecks_before_the_send(
    db_session, db_session_factory
):
    result, create, sent = await _hub_call(
        db_session,
        db_session_factory,
        tool_name="continue_turn",
        change_on=("GET", "/v1/sessions/ses_existing"),
    )
    assert result["result"].get("isError"), result
    create.assert_not_awaited()
    assert sent == []


async def test_hub_admin_demoted_mid_turn_is_held_to_the_pin(db_session, db_session_factory):
    result, create, sent = await _hub_call(
        db_session,
        db_session_factory,
        tool_name="start_turn",
        change_on=("GET", "/v1/environments"),
        admin=True,
    )
    assert result["result"].get("isError"), result
    create.assert_not_awaited()
    assert sent == []
