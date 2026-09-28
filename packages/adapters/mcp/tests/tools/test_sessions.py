from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event import (
    BetaManagedAgentsUserMessageEvent,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.middleware.mcp_identity import (
    IdentityMiddleware,
    production_agent_id_resolver,
    production_internal_resolver,
    production_is_admin_resolver,
    production_role_resolver,
    production_subject_resolver,
    production_tenant_resolver,
)
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.sessions import (
    SessionEventOut,
    SessionInfo,
    _get_session_impl,
    _list_session_events_impl,
    _list_sessions_impl,
    register_sessions_tools,
)
from daimon.core.scope import DeploymentDefault
from daimon.testing import ma_agent, ma_session
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    list_response,
)
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp


def _runtime(client: AsyncAnthropic) -> McpRuntime:
    return McpRuntime(
        session_factory=MagicMock(),
        client=client,  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


# ---------------------------------------------------------------------------
# Full-HTTP-pipeline app — FastMCP
# output-schema validation only runs through mcp.http_app() + a real JSON-RPC
# tools/call; unit-calling the _impl functions bypasses it entirely.
# ---------------------------------------------------------------------------


def _sessions_mcp_app(client: AsyncAnthropic, token: str, claims: dict[str, str]) -> ASGIApp:
    """Assemble a minimal FastMCP app with the sessions tool group registered
    behind the real auth + identity middleware pipeline."""
    mock_sessionmaker: async_sessionmaker[AsyncSession] = MagicMock()  # type: ignore[assignment]
    mcp = FastMCP(name="sessions-schema-test", auth=StaticTokenVerifier(tokens={token: claims}))
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=production_subject_resolver,
            tenant_resolver=production_tenant_resolver,
            role_resolver=production_role_resolver,
            agent_id_resolver=production_agent_id_resolver,
            is_admin_resolver=production_is_admin_resolver,
            internal_resolver=production_internal_resolver,
            sessionmaker=mock_sessionmaker,
        )
    )
    register_sessions_tools(mcp, _runtime(client))
    return mcp.http_app()


def _make_session_payload(
    *,
    session_id: str = "ses_1",
    agent_id: str = "ag_a",
    status: str = "idle",
    account_id: str | None = None,
) -> dict[str, Any]:
    """A session payload with ``status`` overridden after validated construction:
    the SDK's Literal cannot carry a novel value, even though its response-parsing
    path tolerates one at the transport boundary. Session reads are scoped to the
    account that opened the session, so a payload with no ``daimon_account`` is
    unreadable by design."""
    payload = ma_session(
        id=session_id,
        agent_id=agent_id,
        metadata={"daimon_account": account_id} if account_id else {},
    ).model_dump(mode="json")
    payload["status"] = status
    return payload


def _make_thread_idle_event_raw(*, stop_reason_type: str = "end_turn") -> dict[str, Any]:
    """Build a ``session.thread_status_idle`` event as a raw dict — the pinned
    SDK's ``BetaManagedAgentsSessionEvent`` union has no constructor for this
    variant, so validated construction is impossible by definition (mirrors
    test_agent_chat.py's ``_make_thread_idle_event``)."""
    return {
        "id": "sevt_thread_idle_001",
        "content": None,
        "type": "session.thread_status_idle",
        "processed_at": "2026-07-01T13:32:27.914598Z",
        "agent_name": "test-agent",
        "session_thread_id": "sthr_test001",
        "stop_reason": {"type": stop_reason_type},
    }


# ---------------------------------------------------------------------------
# Regression tests — permissive projections through the full pipeline
# ---------------------------------------------------------------------------


async def test_get_session_admits_novel_status_string_through_fastmcp() -> None:
    """get_session must not output-validation-error when MA reports a session
    status the pinned SDK's Literal does not model (e.g. "paused").

    RED on main: SessionInfo.status: Literal["rescheduling", "running", "idle",
    "terminated"] rejects the whole payload with a FastMCP output-validation
    error (isError). Exercised through the HTTP pipeline because FastMCP
    output validation only runs there — unit-calling _get_session_impl
    bypasses the schema check entirely.
    """
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    account_id = uuid.uuid4()
    token = "get-session-novel-status"
    claims = {
        "sub": str(account_id),
        "tenant_id": str(tenant_id),
        "role": "user",
        "client_id": "test",
    }

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=_make_session_payload(
                session_id="ses_1", agent_id="ag_a", status="paused", account_id=str(account_id)
            ),
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    app = _sessions_mcp_app(client, token, claims)

    result = await call_mcp_tool(
        app, token=token, name="get_session", arguments={"session_id": "ses_1"}
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict), f"unexpected tools/call shape: {result!r}"
    assert not payload.get("isError"), (
        f"get_session must not output-validation-error on a novel status string; got {payload!r}"
    )
    structured = payload.get("structuredContent") or {}
    assert structured.get("status") == "paused", (
        f"the novel status must survive in the tool output; got {structured!r}"
    )


async def test_list_session_events_admits_thread_status_events_through_fastmcp() -> None:
    """list_session_events must not output-validation-error on a
    session.thread_status_idle event — a variant the pinned SDK's
    discriminated union does not model.

    RED on main: Page[BetaManagedAgentsSessionEvent] re-validates
    cursor.data against the union on construction and rejects the whole page
    (the #214 failure mode, on the tenant tool surface).
    """
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()
    token = "list-events-novel-type"
    claims = {
        "sub": str(account_id),
        "tenant_id": str(tenant_id),
        "role": "user",
        "client_id": "test",
    }

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=_make_session_payload(
                session_id="ses_1", agent_id="ag_a", account_id=str(account_id)
            ),
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: httpx.Response(
            200,
            json={"data": [_make_thread_idle_event_raw()], "next_page": None},
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    app = _sessions_mcp_app(client, token, claims)

    result = await call_mcp_tool(
        app, token=token, name="list_session_events", arguments={"session_id": "ses_1"}
    )

    payload = result.get("result", result)
    assert isinstance(payload, dict), f"unexpected tools/call shape: {result!r}"
    assert not payload.get("isError"), (
        f"list_session_events must not output-validation-error on a "
        f"thread_status_idle event; got {payload!r}"
    )
    structured = payload.get("structuredContent") or {}
    types = [ev.get("type") for ev in structured.get("items", [])]  # type: ignore[union-attr]
    assert "session.thread_status_idle" in types, (
        f"the thread_status_idle event must survive in the page; got types {types!r}"
    )


async def test_list_sessions_returns_only_tenant_scoped_sessions() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions",
        lambda _r, _m: list_response(
            [
                ma_session(
                    id="ses_1", agent_id="ag_a", metadata={"daimon_account": str(account_id)}
                ).model_dump(mode="json")
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN)
    result = await _list_sessions_impl(_runtime(client), auth, page=None, agent_name=None)

    assert len(result) == 1, "should drain one session for the tenant's only agent"
    assert result[0].id == "ses_1", "should expose the MA session id"
    assert result[0].agent_id == "ag_a", "agent_id should come from session.agent.id"
    assert isinstance(result[0], SessionInfo), "should return SessionInfo projections"


async def test_list_sessions_with_unknown_agent_name_raises() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    # No agents in the tenant -> find_agent_by_daimon_tag returns None.
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response([]))
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN)
    with pytest.raises(ToolError, match="not found"):
        await _list_sessions_impl(_runtime(client), auth, page=None, agent_name="nope")


async def test_get_session_raises_when_session_belongs_to_other_tenant() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    # Tenant has agent ag_a; the requested session is owned by ag_other.
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=ma_session(
                id="ses_x", agent_id="ag_other", metadata={"daimon_account": str(account_id)}
            ).model_dump(mode="json"),
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN)
    with pytest.raises(ToolError, match="session not found"):
        await _get_session_impl(_runtime(client), auth, "ses_x")


async def test_get_session_returns_session_info_when_owned() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=ma_session(
                id="ses_1", agent_id="ag_a", metadata={"daimon_account": str(account_id)}
            ).model_dump(mode="json"),
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN)
    result = await _get_session_impl(_runtime(client), auth, "ses_1")

    assert isinstance(result, SessionInfo), "should return a SessionInfo"
    assert result.id == "ses_1", "should expose the requested session id"
    assert result.agent_id == "ag_a", "tenant ownership check must pass when agent matches"


async def test_list_session_events_returns_page_envelope() -> None:
    tenant_id = uuid.uuid4()
    account_id = uuid.uuid4()

    user_event = BetaManagedAgentsUserMessageEvent.model_validate(
        {
            "id": "sevt_1",
            "type": "user.message",
            "content": [{"type": "text", "text": "hi"}],
        }
    ).model_dump(mode="json")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200,
            json=ma_session(
                id="ses_1", agent_id="ag_a", metadata={"daimon_account": str(account_id)}
            ).model_dump(mode="json"),
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)/events",
        lambda _r, _m: httpx.Response(200, json={"data": [user_event], "next_page": "p2"}),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=account_id, tenant_id=tenant_id, role=Role.ADMIN)
    result = await _list_session_events_impl(
        _runtime(client), auth, "ses_1", page=None, limit=None, order=None
    )

    assert len(result.items) == 1, "should expose the one event from the page"
    assert result.next_page == "p2", "should forward SDK next_page cursor unchanged"
    item: SessionEventOut = result.items[0]
    assert item.id == "sevt_1", "event id should round-trip through SDK parse"


# ---------------------------------------------------------------------------
# Session reads are scoped to the account that opened the session, not the
# tenant. Everyone in an install shares a tenant, so a tenant-only check let
# any member read any other member's transcript.
# ---------------------------------------------------------------------------


def _agents_router(tenant_id: uuid.UUID) -> MARouter:
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda _r, _m: list_response(
            [
                ma_agent(
                    id="ag_a",
                    name="demo",
                    metadata={"daimon_tenant": str(tenant_id), "daimon_name": "demo"},
                ).model_dump(mode="json")
            ]
        ),
    )
    return router


async def test_get_session_refuses_a_session_opened_by_another_member() -> None:
    """The same tenant is not the same person — a DM transcript is not shared."""
    tenant_id = uuid.uuid4()
    owner_account = uuid.uuid4()
    other_account = uuid.uuid4()

    router = _agents_router(tenant_id)
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200, json=_make_session_payload(account_id=str(owner_account))
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=other_account, tenant_id=tenant_id, role=Role.USER)

    with pytest.raises(ToolError, match="session not found"):
        await _get_session_impl(_runtime(client), auth, "ses_1")


async def test_get_session_refuses_another_members_session_even_for_an_admin() -> None:
    """Manage Server governs the server's configuration, not other people's chats."""
    tenant_id = uuid.uuid4()
    owner_account = uuid.uuid4()

    router = _agents_router(tenant_id)
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(
            200, json=_make_session_payload(account_id=str(owner_account))
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    admin = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.ADMIN, is_admin=True
    )

    with pytest.raises(ToolError, match="session not found"):
        await _get_session_impl(_runtime(client), admin, "ses_1")


async def test_get_session_refuses_an_unattributable_session() -> None:
    """Fail closed: a session with no account stamp cannot be shown to anyone."""
    tenant_id = uuid.uuid4()

    router = _agents_router(tenant_id)
    router.add(
        "GET",
        r"/v1/sessions/([^/]+)",
        lambda _r, _m: httpx.Response(200, json=_make_session_payload(account_id=None)),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant_id, role=Role.USER)

    with pytest.raises(ToolError, match="session not found"):
        await _get_session_impl(_runtime(client), auth, "ses_1")


async def test_list_sessions_hides_other_members_sessions() -> None:
    """Enumeration is the same leak as retrieval, so it gets the same filter."""
    tenant_id = uuid.uuid4()
    mine = uuid.uuid4()
    theirs = uuid.uuid4()

    router = _agents_router(tenant_id)
    router.add(
        "GET",
        r"/v1/sessions",
        lambda _r, _m: list_response(
            [
                _make_session_payload(session_id="ses_mine", account_id=str(mine)),
                _make_session_payload(session_id="ses_theirs", account_id=str(theirs)),
                _make_session_payload(session_id="ses_unstamped", account_id=None),
            ]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    auth = AuthIdentity(account_id=mine, tenant_id=tenant_id, role=Role.ADMIN)
    results = await _list_sessions_impl(_runtime(client), auth, None, None)

    assert [r.id for r in results] == ["ses_mine"], (
        "listing must return only sessions this caller opened — not a "
        "co-member's, and not one that cannot be attributed"
    )


@pytest.mark.parametrize("private_stamp", ["dm:discord-route", "", "malformed", "other-grant"])
@pytest.mark.parametrize("admin", [False, True])
def test_private_session_stamp_is_not_account_or_admin_authority(private_stamp, admin):
    from daimon.adapters.mcp.tools.agent_chat import _owned_by_caller
    from daimon.adapters.mcp.tools.hub import _owned_by
    from daimon.adapters.mcp.tools.sessions import _session_belongs_to_caller

    account_id = uuid.uuid4()
    auth = AuthIdentity(
        account_id=account_id,
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN if admin else Role.USER,
        is_admin=admin,
        slack_turn_context_id=uuid.uuid4(),
    )
    session = ma_session(
        metadata={"daimon_account": str(account_id), "daimon_private_dm": private_stamp}
    )
    assert not _session_belongs_to_caller(session, auth)
    assert not _owned_by_caller(session, auth)
    assert not _owned_by(session, auth)
