"""Operator tokens over HTTP: a token sees and calls only its scopes' tools.

Runs the real app (verifier, identity middleware, visibility transforms)
against the test DB, as an integration calling ``/mcp`` would.
"""

from __future__ import annotations

import datetime as dt
import uuid

import jwt as pyjwt
from daimon.adapters.mcp.middleware.mcp_identity import IdentityMiddleware
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.mcp_auth import mint_operator_mcp_token
from daimon.core.operator_tokens import OperatorScope
from daimon.core.stores.security_audit import list_events
from daimon.testing.asgi import call_mcp_tool, mcp_session
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette

from .harness import make_jwt, seed_server_admin

SECRET = "a" * 32


def _make_app(
    sessionmaker: async_sessionmaker[AsyncSession], *, calls_per_minute: int = 60
) -> Starlette:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(
                jwt_secret=SecretStr(SECRET),
                public_url=HttpUrl("https://x/mcp"),
                operator_calls_per_minute=calls_per_minute,
            ),
        ),
        sessionmaker=sessionmaker,
    )


async def _operator_token(
    sessionmaker: async_sessionmaker[AsyncSession], *scopes: OperatorScope
) -> tuple[uuid.UUID, uuid.UUID, str]:
    async with sessionmaker() as s, s.begin():
        tenant_id, account_id = await seed_server_admin(s)
        token = await mint_operator_mcp_token(
            s,
            account_id=account_id,
            tenant_id=tenant_id,
            scopes=frozenset(scopes),
            label=None,
            secret=SECRET.encode(),
            now=dt.datetime.now(dt.UTC),
            ttl_days=30,
        )
    jti = uuid.UUID(pyjwt.decode(token, options={"verify_signature": False})["jti"])
    return tenant_id, jti, token


async def _tool_names(app: Starlette, token: str) -> set[str]:
    result = await mcp_session(app, token=token, method="tools/list")
    payload = result["result"]
    assert isinstance(payload, dict)
    return {tool["name"] for tool in payload["tools"]}  # type: ignore[index]


def _text(result: dict[str, object]) -> str:
    return str(result.get("result", result.get("error")))


async def test_tenant_read_token_lists_exactly_its_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _tenant_id, _jti, token = await _operator_token(sessionmaker, "tenant:read")
    app = _make_app(sessionmaker)

    names = await _tool_names(app, token)

    assert names == {"get_tenant_summary", "list_channel_budgets", "get_channel_budget"}, (
        "an operator token sees only its scopes' tools, without the search collapse"
    )


async def test_operator_token_cannot_call_a_tool_outside_its_scopes(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _tenant_id, _jti, token = await _operator_token(sessionmaker, "tenant:read")
    app = _make_app(sessionmaker)

    result = await call_mcp_tool(
        app,
        token=token,
        name="set_channel_budget",
        arguments={"channel_id": "1", "limit_usd": "5", "window": "monthly"},
    )

    assert "Unknown tool" in _text(result), f"the tool is hidden from this token: {result}"


async def test_narrowing_a_tokens_scopes_applies_to_the_next_request(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _tenant_id, jti, token = await _operator_token(sessionmaker, "tenant:read", "promo:redeem")
    app = _make_app(sessionmaker)
    assert "redeem_promo_code" in await _tool_names(app, token)
    async with sessionmaker() as s, s.begin():
        await s.execute(
            text("UPDATE mcp_tokens SET scopes = :scopes WHERE jti = :jti"),
            {"scopes": ["tenant:read"], "jti": jti},
        )

    assert "redeem_promo_code" not in await _tool_names(app, token), "scopes are read live"


async def test_promo_create_tools_are_hidden_from_server_admins(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as s, s.begin():
        _tenant_id, account_id = await seed_server_admin(s)
    app = _make_app(sessionmaker)
    admin_token = make_jwt(account_id=account_id)

    search = await call_mcp_tool(
        app, token=admin_token, name="search_tools", arguments={"query": "create promo code"}
    )
    call = await call_mcp_tool(
        app,
        token=admin_token,
        name="call_tool",
        arguments={"name": "create_promo_code", "arguments": {"amount_usd": "5", "kind": "credit"}},
    )

    assert "create_promo_code" not in _text(search), "search never offers it to an admin"
    assert "Unknown tool" in _text(call), f"an admin cannot call it: {call}"


async def test_promo_create_token_sees_the_issuing_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _tenant_id, _jti, token = await _operator_token(sessionmaker, "promo:create")
    names = await _tool_names(_make_app(sessionmaker), token)
    assert names == {"create_promo_code", "list_promo_codes", "revoke_promo_code"}


async def test_operator_calls_are_rate_limited_and_audited(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, jti, token = await _operator_token(committing_sessionmaker, "tenant:read")
    app = _make_app(committing_sessionmaker, calls_per_minute=1)

    first = await call_mcp_tool(app, token=token, name="get_tenant_summary")
    second = await call_mcp_tool(app, token=token, name="get_tenant_summary")
    for middleware in app.state.mcp.middleware:
        if isinstance(middleware, IdentityMiddleware):
            await middleware.drain_audit()
    async with committing_sessionmaker() as session:
        events = await list_events(session, tenant_id=tenant_id)

    assert "balance_usd" in _text(first), f"the first call succeeds: {first}"
    assert "too many calls" in _text(second), f"the second is over the limit: {second}"
    calls = [e for e in events if e.tool_name == "get_tenant_summary"]
    assert [(e.outcome, e.token_kind, e.token_jti, e.scope) for e in calls] == [
        ("allowed", "operator", jti, "tenant:read"),
        ("denied", "operator", jti, None),
    ], "each call is audited with the token's kind, jti and the scope checked"
    assert all(e.platform_user_id == "u-admin" for e in calls), "it acts as the admin"
