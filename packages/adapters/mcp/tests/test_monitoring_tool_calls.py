"""MCP monitoring: a person's tool calls on a monitored agent keep name, time and
outcome; the agent's own turns, routines and agent-chat turns are not tool calls."""

from __future__ import annotations

import ast
import datetime as dt
import uuid
from pathlib import Path
from typing import cast

import pytest
from daimon.adapters.mcp.auth.verifier import CHAT_EXECUTION_CLAIM, TOKEN_KIND_CLAIM
from daimon.adapters.mcp.middleware import mcp_identity as identity
from daimon.adapters.mcp.middleware.mcp_identity import (
    MONITORED_QUESTION_TOOLS,
    IdentityMiddleware,
)
from daimon.adapters.mcp.tools import agent_chat
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.monitoring import MCP_NOTICE, KeptQuestion
from daimon.core.security_audit import record_authz_denial
from daimon.core.stores import monitoring as store
from daimon.core.stores.domain import FeatureTarget, MonitoredEventRow
from daimon.core.stores.feature_values import set_feature_value
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_account, make_tenant
from fastmcp import FastMCP
from fastmcp.exceptions import AuthorizationError, ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

AGENT_CHAT = Path(__file__).resolve().parents[1] / "daimon/adapters/mcp/tools/agent_chat.py"
TOKEN = "monitoring-test-credential"


async def _seed(sm: async_sessionmaker[AsyncSession], *, monitored: bool) -> tuple[uuid.UUID, ...]:
    async with sm() as session, session.begin():
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        if monitored:
            await set_feature_value(
                session,
                tenant_id=tenant.id,
                target=FeatureTarget("agent", agent_id="agt_a"),
                key="monitor_mcp",
                value="on",
                set_by=None,
                surface="web",
            )
    return tenant.id, account.id, derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_a")


def _server(
    sm: async_sessionmaker[AsyncSession], tenant: uuid.UUID, account: uuid.UUID, **claims: object
) -> FastMCP:
    mcp = FastMCP(
        "monitoring",
        auth=StaticTokenVerifier(
            tokens={
                TOKEN: {
                    "sub": str(account),
                    "tenant_id": str(tenant),
                    "role": "user",
                    "client_id": "test",
                    TOKEN_KIND_CLAIM: "agent",
                    **claims,
                }
            }
        ),
    )
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=identity.production_subject_resolver,
            tenant_resolver=identity.production_tenant_resolver,
            role_resolver=identity.production_role_resolver,
            agent_id_resolver=identity.production_agent_id_resolver,
            is_admin_resolver=identity.production_is_admin_resolver,
            internal_resolver=identity.production_internal_resolver,
            sessionmaker=sm,
        )
    )

    @mcp.tool(tags={"agent-chat"})
    async def lookup(secret: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return f"result {secret}"

    @mcp.tool(tags={"agent-chat"})
    async def broken(secret: str) -> str:  # pyright: ignore[reportUnusedFunction]
        raise ToolError(f"failed {secret}")

    @mcp.tool(tags={"agent-chat"})
    async def refused_by_policy(secret: str) -> str:  # pyright: ignore[reportUnusedFunction]
        record_authz_denial("post", "own_agents_only")
        raise ToolError(f"not yours {secret}")

    @mcp.tool(tags={"agent-chat"})
    async def refused_by_authorization(secret: str) -> str:  # pyright: ignore[reportUnusedFunction]
        raise AuthorizationError(f"not allowed {secret}")

    @mcp.tool(tags={"agent-chat"})
    async def start_turn(message: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return message

    @mcp.tool(tags={"agent-chat"})
    async def continue_turn(handle: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"handle": handle, "turn_event_id": "t1", "monitoring": MCP_NOTICE.text}

    return mcp


async def _drain(mcp: FastMCP) -> None:
    for middleware in mcp.middleware:
        if isinstance(middleware, IdentityMiddleware):
            await middleware.drain_audit()


async def _calls(
    mcp: FastMCP, sm: async_sessionmaker[AsyncSession], tenant: uuid.UUID, agent: uuid.UUID
) -> list[MonitoredEventRow]:
    app = mcp.http_app()
    await call_mcp_tool(app, token=TOKEN, name="lookup", arguments={"secret": "ARG_SECRET"})
    await call_mcp_tool(app, token=TOKEN, name="broken", arguments={"secret": "ARG_SECRET"})
    await call_mcp_tool(app, token=TOKEN, name="start_turn", arguments={"message": "hi"})
    await _drain(mcp)
    async with sm() as session:
        return await store.list_events(session, tenant_id=tenant, agent_id=agent)


async def test_a_monitored_agents_tool_calls_keep_name_and_outcome_only(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Each call is kept by name and outcome with no arguments or result; the turn tool
    is left to `agent_chat`, which keeps the question and its answer."""
    sm = committing_sessionmaker
    tenant, account, agent = await _seed(sm, monitored=True)
    mcp = _server(sm, tenant, account, agent_id=str(agent))

    rows = await _calls(mcp, sm, tenant, agent)

    assert sorted((row.kind, row.tool_name, row.outcome) for row in rows) == [
        ("notice", None, None),
        ("tool", "broken", "error"),
        ("tool", "lookup", "completed"),
    ], rows
    assert all(row.account_id == account for row in rows), rows
    assert "ARG_SECRET" not in repr(rows) and "result" not in repr(rows), rows


async def test_an_agent_chat_result_that_tells_notes_the_notice(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Noted once the result carrying it is returned, in its conversation and turn."""
    sm = committing_sessionmaker
    tenant, account, agent = await _seed(sm, monitored=True)
    mcp = _server(sm, tenant, account, agent_id=str(agent))
    app = mcp.http_app()
    await call_mcp_tool(app, token=TOKEN, name="continue_turn", arguments={"handle": "s1"})
    await call_mcp_tool(app, token=TOKEN, name="start_turn", arguments={"message": "hi"})
    await _drain(mcp)
    async with sm() as session:
        rows = await store.list_events(session, tenant_id=tenant, agent_id=agent)
    assert [(r.kind, r.conversation, r.turn_ref, r.account_id) for r in rows] == [
        ("notice", "s1", "t1", account)
    ], rows


async def test_refused_calls_are_kept_as_denied_not_completed(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A call the tool refused (a policy denial, an authorization error) or that names a
    tool the key cannot see is kept as denied; only a call that ran reads completed."""
    sm = committing_sessionmaker
    tenant, account, agent = await _seed(sm, monitored=True)
    mcp = _server(sm, tenant, account, agent_id=str(agent))
    app = mcp.http_app()

    for name in ("lookup", "refused_by_policy", "refused_by_authorization", "no_such_tool"):
        await call_mcp_tool(app, token=TOKEN, name=name, arguments={"secret": "ARG_SECRET"})
    await _drain(mcp)
    async with sm() as session:
        rows = await store.list_events(session, tenant_id=tenant, agent_id=agent)

    assert sorted((row.tool_name, row.outcome) for row in rows if row.kind == "tool") == [
        ("lookup", "completed"),
        ("no_such_tool", "denied"),
        ("refused_by_authorization", "denied"),
        ("refused_by_policy", "denied"),
    ], rows
    assert "ARG_SECRET" not in repr(rows), "a refused call kept its arguments"


def _texts(response: dict[str, object]) -> list[str]:
    result = cast("dict[str, list[dict[str, str]]]", response["result"])
    return [block["text"] for block in result["content"]]


def _structured(response: dict[str, object]) -> object:
    return cast("dict[str, object]", response["result"]).get("structuredContent")


async def test_the_first_kept_call_tells_the_person(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Its result carries the notice as one more text block, structured content left as
    it is; a call before it, which could not tell them, is not kept."""
    sm = committing_sessionmaker
    tenant, account, agent = await _seed(sm, monitored=True)
    mcp = _server(sm, tenant, account, agent_id=str(agent))
    app = mcp.http_app()
    args: dict[str, object] = {"secret": "x"}

    await call_mcp_tool(app, token=TOKEN, name="broken", arguments=args)
    first = await call_mcp_tool(app, token=TOKEN, name="lookup", arguments=args)
    second = await call_mcp_tool(app, token=TOKEN, name="lookup", arguments=args)
    await _drain(mcp)
    async with sm() as session:
        rows = await store.list_events(session, tenant_id=tenant, agent_id=agent)

    assert _texts(first) == ["result x", MCP_NOTICE.text], first
    assert _texts(second) == ["result x"], second
    assert _structured(first) == _structured(second), first
    assert sorted((row.kind, row.tool_name) for row in rows) == [
        ("notice", None),
        ("tool", "lookup"),
        ("tool", "lookup"),
    ], rows


@pytest.mark.parametrize(
    "claims",
    [
        {CHAT_EXECUTION_CLAIM: True},
        {"routine_id": "rtn_1"},
        {"execution_id": "exec_1"},
    ],
    ids=["chat-execution", "routine", "execution"],
)
async def test_an_agents_own_calls_are_not_the_persons(
    committing_sessionmaker: async_sessionmaker[AsyncSession], claims: dict[str, object]
) -> None:
    """A token the agent's own turn or a routine carries keeps nothing."""
    sm = committing_sessionmaker
    tenant, account, agent = await _seed(sm, monitored=True)
    mcp = _server(sm, tenant, account, agent_id=str(agent), **claims)

    assert await _calls(mcp, sm, tenant, agent) == [], f"{claims} calls were kept"


async def test_an_agent_turn_token_keeps_nothing(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A `chat_agent_id` token is the agent's own turn calling tools, never a person."""
    sm = committing_sessionmaker
    tenant, account, agent = await _seed(sm, monitored=True)
    mcp = _server(sm, tenant, account, chat_agent_id=str(agent))

    assert await _calls(mcp, sm, tenant, agent) == [], "an agent turn's calls were kept"


async def test_an_unmonitored_agent_keeps_no_tool_calls(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """With `monitor_mcp` off, the person's calls leave no row."""
    sm = committing_sessionmaker
    tenant, account, agent = await _seed(sm, monitored=False)
    mcp = _server(sm, tenant, account, agent_id=str(agent))

    assert await _calls(mcp, sm, tenant, agent) == [], "off kept tool calls"


def test_person_agent_needs_an_agent_key() -> None:
    """Only an agent-kind key with no turn or routine claim is a person on that agent."""
    agent = uuid.uuid4()
    person = identity._person_agent  # pyright: ignore[reportPrivateUsage]
    assert person(agent, {TOKEN_KIND_CLAIM: "agent"}) == agent
    assert person(None, {TOKEN_KIND_CLAIM: "agent"}) is None, "no agent, no subject"
    assert person(agent, {TOKEN_KIND_CLAIM: "user"}) is None, "a user key names no agent"
    assert person(agent, {}) is None, "an unverified token is not a key"


def test_every_agent_chat_send_keeps_the_question() -> None:
    """Each agent-chat function that sends a person's message keeps it after the send,
    and each turn tool is one the middleware leaves out of tool calls."""
    tree = ast.parse(AGENT_CHAT.read_text())

    def calls(node: ast.AST, name: str) -> list[int]:
        return [
            n.lineno
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == name
        ]

    senders = [
        fn
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef)
        and fn.name != "_send_billed"
        and calls(fn, "_send_billed")
    ]
    assert len(senders) >= 2, [fn.name for fn in senders]
    for fn in senders:
        sends, keeps = calls(fn, "_send_billed"), calls(fn, "_keep_question")
        assert len(keeps) >= len(sends) and min(keeps) > min(sends), (
            f"{fn.name} sends a message without keeping it after the send"
        )
    turn_tools = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef)
        and any(
            name in {"_start_turn_impl", "_continue_turn_impl", "_ask_impl"}
            for name in (
                getattr(n.func, "id", None) for n in ast.walk(fn) if isinstance(n, ast.Call)
            )
        )
        and not fn.name.startswith("_")
    }
    assert turn_tools and turn_tools <= MONITORED_QUESTION_TOOLS, turn_tools


def test_agent_chat_results_lead_with_the_monitoring_notice() -> None:
    """A told turn's handle and `ask` result carry the notice; later turns do not."""
    now = dt.datetime.now(dt.UTC)
    told = agent_chat._turn_handle("h", "t1", now, KeptQuestion("t1", MCP_NOTICE))  # pyright: ignore[reportPrivateUsage]
    quiet = agent_chat._turn_handle("h", "t2", now, KeptQuestion("t2", None))  # pyright: ignore[reportPrivateUsage]
    assert told["monitoring"] == MCP_NOTICE.text and "monitoring" not in quiet
    answer = agent_chat.AskResult(handle="h", message="hi", monitoring=told["monitoring"])
    result = agent_chat._ask_tool_result(answer)  # pyright: ignore[reportPrivateUsage]
    assert [getattr(c, "text", None) for c in result.content] == [MCP_NOTICE.text, "hi"]
