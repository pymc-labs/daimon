"""The thin per-tool wrappers in register_hub_tools resolve, delegate, and reject
foreign daimon ids the same way ask does -- driven over the wire, not called directly."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsSession
from daimon.adapters.mcp.hub.app import build_hub_app
from daimon.adapters.mcp.hub.claims import encode_hub_claims
from daimon.core.hub_identity import HubTenant
from daimon.core.ma_identity import derive_agent_uuid
from daimon.testing.factories import make_platform_principal, make_tenant
from daimon.testing.ma import build_fake_anthropic, list_response, session_response
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_turn_parity import AGENT_ID, _call_tool, _runtime, build_turn_router

pytestmark = pytest.mark.asyncio

_TOKEN = "tok"
_HANDLE = "ses_wrap_001"


def _foreign_daimon_id() -> str:
    """A well-formed daimon_id derived from a tenant that isn't in the caller's
    HubIdentity -- indistinguishable from a nonexistent one to _resolve_daimon."""
    return str(derive_agent_uuid(tenant_id=uuid.uuid4(), ma_agent_id=AGENT_ID))


def _session_payload(session_id: str) -> dict[str, Any]:
    return BetaManagedAgentsSession.model_validate(
        {
            "id": session_id,
            "type": "session",
            "agent": {
                "id": AGENT_ID,
                "name": "test-agent",
                "version": 1,
                "type": "agent",
                "model": {"id": "claude-sonnet-4-6"},
                "mcp_servers": [],
                "skills": [],
                "tools": [],
            },
            "archived_at": None,
            "created_at": "2026-06-23T00:00:00Z",
            "updated_at": "2026-06-23T00:00:00Z",
            "outcome_evaluations": [],
            "environment_id": "env_parity_test",
            "metadata": {},
            "resources": [],
            "stats": {},
            "status": "idle",
            "title": None,
            "usage": {},
            "vault_ids": [],
        }
    ).model_dump(mode="json")


async def _hub_app(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> tuple[Any, str, HubTenant]:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="u1", tenant=tenant
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
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)$",
        lambda _r, m: session_response(session_id=m.group(1), status="idle", agent_id=AGENT_ID),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events$",
        lambda _r, _m: httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "sevt_wrap_msg",
                        "type": "agent.message",
                        "content": [{"type": "text", "text": "hi"}],
                    }
                ],
                "next_page": None,
            },
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions$",
        lambda _r, _m: list_response([_session_payload("ses_a"), _session_payload("ses_b")]),
    )
    runtime = _runtime(build_fake_anthropic(router.dispatch), db_session_factory)
    mcp = build_hub_app(platform="discord", runtime=runtime, auth=auth, billing_config=None)
    daimon_id = str(derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=AGENT_ID))
    return mcp, daimon_id, hub_tenant


async def test_describe_daimon_returns_the_platform_and_workspace_the_caller_addressed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mcp, daimon_id, hub_tenant = await _hub_app(db_session, db_session_factory)
    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    result = await _call_tool(
        app, "/mcp", _TOKEN, name="describe_daimon", arguments={"daimon_id": daimon_id}
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict) and not payload.get("isError"), (
        f"describe_daimon call failed: {payload!r}"
    )
    described = payload.get("structuredContent") or {}
    assert described.get("platform") == "discord", f"got {described!r}"
    assert described.get("workspace_id") == hub_tenant.workspace_id, f"got {described!r}"
    assert described.get("workspace") == hub_tenant.workspace_name, f"got {described!r}"


async def test_describe_daimon_rejects_a_daimon_id_outside_the_callers_tenants(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mcp, _daimon_id, _hub_tenant = await _hub_app(db_session, db_session_factory)
    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    result = await _call_tool(
        app,
        "/mcp",
        _TOKEN,
        name="describe_daimon",
        arguments={"daimon_id": _foreign_daimon_id()},
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict) and payload.get("isError"), (
        f"describe_daimon must reject a daimon_id outside the caller's tenants, got {payload!r}"
    )
    assert "daimon not found" in str(payload.get("content")), f"got {payload!r}"


async def test_get_session_returns_the_session_status(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mcp, daimon_id, _hub_tenant = await _hub_app(db_session, db_session_factory)
    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    result = await _call_tool(
        app,
        "/mcp",
        _TOKEN,
        name="get_session",
        arguments={"daimon_id": daimon_id, "handle": _HANDLE},
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict) and not payload.get("isError"), (
        f"get_session call failed: {payload!r}"
    )
    info = payload.get("structuredContent") or {}
    assert info.get("id") == _HANDLE, f"got {info!r}"
    assert info.get("status") == "idle", f"got {info!r}"


async def test_get_session_rejects_a_daimon_id_outside_the_callers_tenants(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mcp, _daimon_id, _hub_tenant = await _hub_app(db_session, db_session_factory)
    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    result = await _call_tool(
        app,
        "/mcp",
        _TOKEN,
        name="get_session",
        arguments={"daimon_id": _foreign_daimon_id(), "handle": _HANDLE},
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict) and payload.get("isError"), (
        f"get_session must reject a daimon_id outside the caller's tenants, got {payload!r}"
    )
    assert "daimon not found" in str(payload.get("content")), f"got {payload!r}"


async def test_list_events_returns_the_sessions_transcript(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mcp, daimon_id, _hub_tenant = await _hub_app(db_session, db_session_factory)
    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    result = await _call_tool(
        app,
        "/mcp",
        _TOKEN,
        name="list_events",
        arguments={"daimon_id": daimon_id, "handle": _HANDLE},
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict) and not payload.get("isError"), (
        f"list_events call failed: {payload!r}"
    )
    page = payload.get("structuredContent") or {}
    items = page.get("items") or []
    assert any(item.get("type") == "agent.message" for item in items), f"got {items!r}"


async def test_list_events_rejects_a_daimon_id_outside_the_callers_tenants(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mcp, _daimon_id, _hub_tenant = await _hub_app(db_session, db_session_factory)
    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    result = await _call_tool(
        app,
        "/mcp",
        _TOKEN,
        name="list_events",
        arguments={"daimon_id": _foreign_daimon_id(), "handle": _HANDLE},
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict) and payload.get("isError"), (
        f"list_events must reject a daimon_id outside the caller's tenants, got {payload!r}"
    )
    assert "daimon not found" in str(payload.get("content")), f"got {payload!r}"


async def test_list_my_sessions_returns_the_callers_sessions(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mcp, daimon_id, _hub_tenant = await _hub_app(db_session, db_session_factory)
    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    result = await _call_tool(
        app, "/mcp", _TOKEN, name="list_my_sessions", arguments={"daimon_id": daimon_id}
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict) and not payload.get("isError"), (
        f"list_my_sessions call failed: {payload!r}"
    )
    structured = payload.get("structuredContent") or {}
    sessions = structured.get("result", structured) if isinstance(structured, dict) else structured
    assert {s["id"] for s in sessions} == {"ses_a", "ses_b"}, f"got {sessions!r}"


async def test_list_my_sessions_rejects_a_daimon_id_outside_the_callers_tenants(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mcp, _daimon_id, _hub_tenant = await _hub_app(db_session, db_session_factory)
    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    result = await _call_tool(
        app,
        "/mcp",
        _TOKEN,
        name="list_my_sessions",
        arguments={"daimon_id": _foreign_daimon_id()},
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict) and payload.get("isError"), (
        f"list_my_sessions must reject a daimon_id outside the caller's tenants, got {payload!r}"
    )
    assert "daimon not found" in str(payload.get("content")), f"got {payload!r}"
