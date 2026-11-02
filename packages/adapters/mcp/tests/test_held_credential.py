"""A held turn's credential: verifier, identity and the tool allowlist.

Verifier tests call `verify_token` on real minted tokens. Identity and
visibility tests go over the ASGI transport, since session visibility rules
need a real session (see `test_rbac`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast

import jwt as pyjwt
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.adapters.mcp.middleware.held_tools import (
    HELD_PROXY_TOOLS,
    HELD_TOOL_ALLOWLIST,
    held_tool_refusal,
)
from daimon.adapters.mcp.middleware.mcp_identity import (
    IdentityMiddleware,
    production_agent_id_resolver,
    production_internal_resolver,
    production_is_admin_resolver,
    production_role_resolver,
    production_subject_resolver,
    production_tenant_resolver,
)
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.mcp_auth import mint_agent_mcp_token, mint_chat_mcp_token
from daimon.core.mcp_hold import HOLD_CLAIM
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import McpTokenRow, Role
from daimon.core.turn_hold import TurnHold
from daimon.testing.asgi import mcp_session
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.context import Context
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

SECRET = b"a" * 32
HOLD = TurnHold(platform="slack", channel_id="C-SYN-1", client=True)
OTHER = TurnHold(platform="slack", channel_id="C-SYN-2", client=True)


def _verifier(sessionmaker: async_sessionmaker[AsyncSession]) -> DaimonJWTVerifier:
    return DaimonJWTVerifier(secret=SECRET, sessionmaker=sessionmaker)


async def _slack_account(
    sessionmaker: async_sessionmaker[AsyncSession], *, role: Role = Role.USER
) -> uuid.UUID:
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform="slack", workspace_id=f"T-{uuid.uuid4().hex[:8]}")
        account = await make_account(s, tenant=tenant)
        await make_platform_principal(
            s, platform="slack", external_id="U-SYN", tenant=tenant, account=account
        )
        if role is not Role.USER:
            await set_role(s, account.id, role)
    return account.id


async def _chat_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    hold: TurnHold | None,
) -> str:
    async with sessionmaker() as s, s.begin():
        token, _, _ = await mint_chat_mcp_token(
            s,
            account_id=account_id,
            agent_id=uuid.uuid4(),
            secret=SECRET,
            now=dt.datetime.now(dt.UTC),
            hold=hold,
        )
    return token


def _resign(token: str, mutate: dict[str, object | None]) -> str:
    claims = pyjwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})
    for key, value in mutate.items():
        if value is None:
            claims.pop(key, None)
        else:
            claims[key] = value
    return pyjwt.encode(claims, SECRET, algorithm="HS256")


async def test_held_token_round_trips_through_the_verifier(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account_id = await _slack_account(sessionmaker)
    token = await _chat_token(sessionmaker, account_id, HOLD)

    access = await _verifier(sessionmaker).verify_token(token)

    assert access is not None
    assert access.claims[HOLD_CLAIM] == {
        "platform": "slack",
        "channel_id": "C-SYN-1",
        "client": True,
    }
    assert access.claims["bound_channel_id"] == "C-SYN-1"


async def test_held_token_of_a_non_admin_is_not_refused_for_its_channel_binding(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`admin_required` stays for agent keys; it must not reject a held chat token."""
    account_id = await _slack_account(sessionmaker, role=Role.USER)
    assert await _verifier(sessionmaker).verify_token(
        await _chat_token(sessionmaker, account_id, HOLD)
    )


async def test_unheld_chat_token_is_unchanged(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account_id = await _slack_account(sessionmaker)
    token = await _chat_token(sessionmaker, account_id, None)

    access = await _verifier(sessionmaker).verify_token(token)

    assert access is not None
    assert HOLD_CLAIM not in access.claims
    assert "bound_channel_id" not in access.claims


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            {HOLD_CLAIM: {"platform": "slack", "channel_id": "C-SYN-2", "client": True}},
            id="claim-names-another-channel",
        ),
        pytest.param(
            {HOLD_CLAIM: {"platform": "teams", "channel_id": "C-SYN-1", "client": True}},
            id="claim-names-another-platform",
        ),
        pytest.param({HOLD_CLAIM: None}, id="claim-missing-on-bound-row"),
        pytest.param({HOLD_CLAIM: {"platform": "slack"}}, id="claim-malformed"),
        pytest.param({HOLD_CLAIM: "slack:C-SYN-1"}, id="claim-wrong-type"),
    ],
)
async def test_held_row_without_a_matching_claim_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], mutation: dict[str, object | None]
) -> None:
    account_id = await _slack_account(sessionmaker)
    token = _resign(await _chat_token(sessionmaker, account_id, HOLD), mutation)

    assert await _verifier(sessionmaker).verify_token(token) is None


async def test_claim_on_an_unheld_row_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account_id = await _slack_account(sessionmaker)
    token = _resign(
        await _chat_token(sessionmaker, account_id, None),
        {HOLD_CLAIM: {"platform": "slack", "channel_id": "C-SYN-1", "client": False}},
    )

    assert await _verifier(sessionmaker).verify_token(token) is None


async def test_tampered_claim_without_the_secret_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account_id = await _slack_account(sessionmaker)
    token = await _chat_token(sessionmaker, account_id, HOLD)
    claims = pyjwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})
    claims[HOLD_CLAIM]["client"] = False
    forged = pyjwt.encode(claims, b"b" * 32, algorithm="HS256")

    assert await _verifier(sessionmaker).verify_token(forged) is None


async def test_agent_key_cannot_carry_a_hold_claim(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account_id = await _slack_account(sessionmaker, role=Role.ADMIN)
    async with sessionmaker() as s, s.begin():
        tenant_id = await make_account_tenant(s, account_id)
        key = await mint_agent_mcp_token(
            s,
            account_id=account_id,
            tenant_id=tenant_id,
            agent_id=uuid.uuid4(),
            label="synthetic",
            secret=SECRET,
            now=dt.datetime.now(dt.UTC),
        )
    forged = _resign(key, {HOLD_CLAIM: {"platform": "slack", "channel_id": "C", "client": False}})

    assert await _verifier(sessionmaker).verify_token(key) is not None
    assert await _verifier(sessionmaker).verify_token(forged) is None


async def make_account_tenant(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID:
    from daimon.core.stores.accounts import get_account_with_tenant

    identity = await get_account_with_tenant(session, account_id=account_id)
    assert identity is not None
    return identity.tenant_id


# --- identity -----------------------------------------------------------------


def _identity_app(
    sessionmaker: async_sessionmaker[AsyncSession], seen: list[AuthIdentity]
) -> ASGIApp:
    claims = {
        "sub": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "role": "admin",
        "platform": "slack",
        "chat_agent_id": str(uuid.uuid4()),
        HOLD_CLAIM: {"platform": "slack", "channel_id": "C-SYN-1", "client": True},
    }
    mcp = FastMCP(
        name="held-identity",
        auth=StaticTokenVerifier(tokens={"t": {"client_id": "c", "scopes": [], **claims}}),
    )
    mcp.add_middleware(
        IdentityMiddleware(
            subject_resolver=production_subject_resolver,
            tenant_resolver=production_tenant_resolver,
            role_resolver=production_role_resolver,
            agent_id_resolver=production_agent_id_resolver,
            is_admin_resolver=production_is_admin_resolver,
            internal_resolver=production_internal_resolver,
            sessionmaker=sessionmaker,
        )
    )

    @mcp.tool
    async def now(ctx: Context) -> str:  # pyright: ignore[reportUnusedFunction]
        seen.append(await ctx.get_state("auth"))
        return "ok"

    @mcp.tool(tags={"admin"})
    async def admin_only() -> str:  # pyright: ignore[reportUnusedFunction]
        return "admin"

    return mcp.http_app()  # pyright: ignore[reportReturnType]


async def test_held_identity_is_never_admin_whatever_the_stored_role(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    seen: list[AuthIdentity] = []
    app = _identity_app(sessionmaker, seen)

    await mcp_session(app, token="t", method="tools/call", params={"name": "now", "arguments": {}})
    listed = await mcp_session(app, token="t", method="tools/list")
    names = {t["name"] for t in listed["result"]["tools"]}  # type: ignore[index]

    (identity,) = seen
    assert identity.role is Role.USER, "held execution has no effective admin role"
    assert identity.is_admin is False
    assert identity.hold == HOLD
    assert identity.client is True
    assert "admin_only" not in names


# --- allowlist and visibility -------------------------------------------------

_SETTINGS = Settings(
    database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
    anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
    mcp=McpSettings(
        legacy_token_cutoff=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        allow_legacy_tokens=True,
        jwt_secret=SecretStr(SECRET.decode()),
        public_url=HttpUrl("https://x/mcp"),
    ),
)


async def _search_names(app: ASGIApp, token: str, query: str) -> set[str]:
    result = await mcp_session(
        app,
        token=token,
        method="tools/call",
        params={"name": "search_tools", "arguments": {"query": query}},
    )
    content = cast("list[dict[str, str]]", result["result"]["content"])  # type: ignore[index]
    text = " ".join(item.get("text", "") for item in content)
    return {line[4:].strip() for line in text.splitlines() if line.startswith("### ")}


@pytest.mark.usefixtures("platform_admin_edge")
async def test_held_session_lists_and_searches_only_allowlisted_tools(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account_id = await _slack_account(sessionmaker, role=Role.ADMIN)
    held = await _chat_token(sessionmaker, account_id, HOLD)
    unheld = await _chat_token(sessionmaker, account_id, None)
    app = create_mcp_app(settings=_SETTINGS, sessionmaker=sessionmaker)

    listed = await mcp_session(app, token=held, method="tools/list")
    held_listed = {t["name"] for t in listed["result"]["tools"]}  # type: ignore[index]
    assert held_listed <= set(HELD_TOOL_ALLOWLIST) | HELD_PROXY_TOOLS

    for query in ("read channel messages", "list agents", "create routine", "archive agent"):
        assert await _search_names(app, held, query) <= set(HELD_TOOL_ALLOWLIST)
    # Channel tools are not registered without a chat platform's settings here,
    # so the positive case is a tool this app does register.
    assert "where_am_i" in await _search_names(app, held, "where am i")

    # An unheld session of the same admin account is unaffected.
    assert "list_agents" in await _search_names(app, unheld, "list agents")
    assert "archive_agent" in await _search_names(app, unheld, "archive agent")


@pytest.mark.parametrize(
    "name", ["list_agents", "create_routine", "archive_agent", "get_cli_token"]
)
async def test_held_call_to_a_tool_off_the_allowlist_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], name: str
) -> None:
    account_id = await _slack_account(sessionmaker, role=Role.ADMIN)
    held = await _chat_token(sessionmaker, account_id, HOLD)
    app = create_mcp_app(settings=_SETTINGS, sessionmaker=sessionmaker)

    direct = await mcp_session(
        app, token=held, method="tools/call", params={"name": name, "arguments": {}}
    )
    proxied = await mcp_session(
        app,
        token=held,
        method="tools/call",
        params={"name": "call_tool", "arguments": {"name": name, "arguments": {}}},
    )

    for result in (direct, proxied):
        body = result.get("result", result)
        text = str(body).lower()
        assert "limited to its own channel" in text or "unknown tool" in text or "not found" in text


def test_allowlist_refusal_is_a_plain_message_and_names_nothing_sensitive() -> None:
    assert held_tool_refusal("read_channel") is None
    assert held_tool_refusal("call_tool") is None
    refusal = held_tool_refusal("list_agents")
    assert refusal is not None and "own channel" in refusal


def test_allowlist_excludes_the_administrative_and_directory_tools() -> None:
    forbidden = {
        "send_direct_message",
        "list_agents",
        "get_agent",
        "create_agent",
        "update_agent",
        "fork_agent",
        "list_skills",
        "get_skill",
        "list_environments",
        "get_tenant_summary",
        "create_routine",
        "list_routines",
        "update_routine",
        "delete_routine",
        "get_routine",
        "request_agent_key",
        "request_mcp_token",
        "request_mcp_oauth",
        "get_cli_token",
        "delete_report",
        "add_skill",
        "github_connect",
        "request_github_access",
        "set_working_repo",
        "set_channel_budget",
        "set_channel_rule",
        "list_agent_keys",
        "remove_agent_key",
        "create_file_upload_url",
    }
    assert not forbidden & set(HELD_TOOL_ALLOWLIST)


# --- a principal marked foreign is narrowed on an unheld credential too ---------


async def _foreign_slack_account(
    sessionmaker: async_sessionmaker[AsyncSession], *, role: Role = Role.USER
) -> uuid.UUID:
    from daimon.core.stores.identity import mark_principal_foreign

    account_id = await _slack_account(sessionmaker, role=role)
    async with sessionmaker() as s, s.begin():
        row = (
            await s.execute(
                text("select id from platform_principals where account_id = :a"),
                {"a": account_id},
            )
        ).scalar_one()
        await mark_principal_foreign(s, principal_id=row, home_id="T-SYN-OTHER")
    return account_id


async def test_a_foreign_principal_with_an_unheld_token_sees_only_the_held_allowlist(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account_id = await _foreign_slack_account(sessionmaker, role=Role.ADMIN)
    unheld = await _chat_token(sessionmaker, account_id, None)
    app = create_mcp_app(settings=_SETTINGS, sessionmaker=sessionmaker)

    listed = await mcp_session(app, token=unheld, method="tools/list")
    names = {t["name"] for t in listed["result"]["tools"]}  # type: ignore[index]
    assert names <= set(HELD_TOOL_ALLOWLIST) | HELD_PROXY_TOOLS

    for name in ("get_cli_token", "list_environments", "list_agents"):
        for params in (
            {"name": name, "arguments": {}},
            {"name": "call_tool", "arguments": {"name": name, "arguments": {}}},
        ):
            result = await mcp_session(app, token=unheld, method="tools/call", params=params)
            assert (
                "limited to its own channel" in str(result).lower()
                or "not found" in str(result).lower()
            ), (name, result)


async def test_an_unmarked_principal_with_an_unheld_token_is_unchanged(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account_id = await _slack_account(sessionmaker, role=Role.ADMIN)
    unheld = await _chat_token(sessionmaker, account_id, None)
    app = create_mcp_app(settings=_SETTINGS, sessionmaker=sessionmaker)

    assert "list_agents" in await _search_names(app, unheld, "list agents")


# --- publishing ---------------------------------------------------------------


def test_publish_report_is_not_on_the_held_allowlist() -> None:
    """It publishes an agent's reader: a copy of its prompt and skills and an agent token."""
    assert "publish_report" not in HELD_TOOL_ALLOWLIST
    assert held_tool_refusal("publish_report") is not None
    # The upload-URL tools stay: they create no agent and no token.
    assert held_tool_refusal("create_notebook_upload_url") is None
    assert held_tool_refusal("create_attachment_upload_url") is None


async def test_a_held_call_cannot_publish_a_report_or_a_blog_or_an_editable_notebook(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    account_id = await _slack_account(sessionmaker)
    held = await _chat_token(sessionmaker, account_id, HOLD)
    app = create_mcp_app(settings=_SETTINGS, sessionmaker=sessionmaker)

    report = await mcp_session(
        app, token=held, method="tools/call", params={"name": "publish_report", "arguments": {}}
    )
    assert "limited to its own channel" in str(report).lower()
    for flag in ("permanent", "editable"):
        result = await mcp_session(
            app,
            token=held,
            method="tools/call",
            params={"name": "create_notebook_upload_url", "arguments": {flag: True}},
        )
        assert "scratch, read-only" in str(result).lower(), result


# --- rollback: the previous verifier must refuse a held token --------------------


def _previous_token_row_refusal(
    row: McpTokenRow, *, claims: dict[str, object], identity: Any, now: dt.datetime
) -> str | None:
    """`token_row_refusal` of the build before held credentials (main d568b40a9), verbatim
    for the agent branch: no hold claim, and only `vault-chat:` marks a vault chat token."""
    from daimon.core.mcp_auth import (
        CHAT_TOKEN_TTL,
        chat_token_label,
        registered_agent_claims_match,
    )

    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at is not None and row.expires_at <= now:
        return "expired"
    if row.platform is not None and row.platform != identity.platform:
        return "platform_mismatch"
    assert row.kind == "agent"
    if row.account_id != identity.account_id or row.tenant_id != identity.tenant_id:
        return "account_mismatch"
    chat = claims.get("chat_agent_id")
    if row.label == chat_token_label(row.jti):
        expiry, issued = claims.get("exp"), claims.get("iat")
        if (
            chat != row.agent_id
            or claims.get("agent_id") is not None
            or claims.get("internal") is not None
            or type(expiry) is not int
            or type(issued) is not int
            or row.expires_at is None
            or expiry != int(row.expires_at.timestamp())
            or not 0 < expiry - issued <= int(CHAT_TOKEN_TTL.total_seconds())
        ):
            return "chat_binding_mismatch"
    elif chat is not None:
        if row.scopes or not registered_agent_claims_match(
            row, claims, now=now, chat_execution=True
        ):
            return "chat_binding_mismatch"
    elif claims.get("agent_id") != row.agent_id:
        return "agent_mismatch"
    if row.channel_id is not None and identity.role is not Role.ADMIN:
        return "admin_required"
    return None


async def test_the_previous_verifier_refuses_a_held_token_and_still_admits_an_unheld_one(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.mcp_auth import CHAT_TOKEN_LABEL_PREFIX, HELD_CHAT_TOKEN_LABEL_PREFIX
    from daimon.core.stores.accounts import get_account_with_tenant
    from daimon.core.stores.mcp_tokens import get_mcp_token

    account_id = await _slack_account(sessionmaker, role=Role.ADMIN)
    held, unheld = (
        await _chat_token(sessionmaker, account_id, HOLD),
        await _chat_token(sessionmaker, account_id, None),
    )
    async with sessionmaker() as s:
        identity = await get_account_with_tenant(s, account_id=account_id)
        assert identity is not None
        rows = {}
        for name, token in (("held", held), ("unheld", unheld)):
            claims = pyjwt.decode(
                token, SECRET, algorithms=["HS256"], options={"verify_exp": False}
            )
            row = await get_mcp_token(s, jti=uuid.UUID(claims["jti"]))
            assert row is not None
            rows[name] = (row, claims)
    now = dt.datetime.now(dt.UTC)

    held_row, held_claims = rows["held"]
    unheld_row, unheld_claims = rows["unheld"]
    assert held_row.label.startswith(HELD_CHAT_TOKEN_LABEL_PREFIX)
    assert unheld_row.label.startswith(CHAT_TOKEN_LABEL_PREFIX)
    assert not held_row.label.startswith(CHAT_TOKEN_LABEL_PREFIX)
    assert (
        _previous_token_row_refusal(unheld_row, claims=unheld_claims, identity=identity, now=now)
        is None
    )
    assert (
        _previous_token_row_refusal(held_row, claims=held_claims, identity=identity, now=now)
        == "chat_binding_mismatch"
    )
    # The hazard this closes: under the ordinary label the previous build admits it.
    relabelled = held_row.model_copy(update={"label": CHAT_TOKEN_LABEL_PREFIX + str(held_row.jti)})
    assert (
        _previous_token_row_refusal(relabelled, claims=held_claims, identity=identity, now=now)
        is None
    )


async def test_the_new_verifier_ties_the_held_label_to_the_hold_claim(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.adapters.mcp.auth.verifier import token_row_refusal
    from daimon.core.mcp_auth import CHAT_TOKEN_LABEL_PREFIX
    from daimon.core.stores.accounts import get_account_with_tenant
    from daimon.core.stores.mcp_tokens import get_mcp_token

    account_id = await _slack_account(sessionmaker)
    token = await _chat_token(sessionmaker, account_id, HOLD)
    claims = pyjwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})
    async with sessionmaker() as s:
        identity = await get_account_with_tenant(s, account_id=account_id)
        row = await get_mcp_token(s, jti=uuid.UUID(claims["jti"]))
    assert identity is not None and row is not None
    now = dt.datetime.now(dt.UTC)
    hold_claim = claims.pop(HOLD_CLAIM)

    assert (
        token_row_refusal(row, claims=claims, identity=identity, now=now, hold_claim=hold_claim)
        is None
    )
    assert (
        token_row_refusal(row, claims=claims, identity=identity, now=now, hold_claim=None)
        == "hold_claim_missing"
    )
    ordinary = row.model_copy(update={"label": CHAT_TOKEN_LABEL_PREFIX + str(row.jti)})
    assert (
        token_row_refusal(
            ordinary, claims=claims, identity=identity, now=now, hold_claim=hold_claim
        )
        == "hold_claim_unexpected"
    )
