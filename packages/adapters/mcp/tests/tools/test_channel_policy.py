"""SYS-029: the tenant channel policy, applied by the shared channel dispatcher.

Driven through the registered tools (JWT verifier, IdentityMiddleware, real
Postgres), with only the platform HTTP faked at the transport, so the
dispatcher's policy load, the turn-origin lookup and each platform's own
caller check all run for real.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
import uuid
from pathlib import Path
from typing import Any

import discord
import discord.http
import pytest
from aioresponses import aioresponses
from cryptography.fernet import Fernet
from daimon.adapters.mcp.server import create_mcp_app
from daimon.adapters.mcp.tools._channel_policy import ChannelReadPolicy
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
    SlackSettings,
)
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_jwt
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.turn_origins import create_origin
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_platform_principal, make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp
from yarl import URL

_conftest_path = Path(__file__).parent / "conftest.py"
_spec = importlib.util.spec_from_file_location("_tools_conftest_policy", _conftest_path)
assert _spec is not None and _spec.loader is not None
_tools_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tools_conftest)
patch_discord_http = _tools_conftest.patch_discord_http
# The full Discord payload builders, loaded the same way (not a package).
_discord_spec = importlib.util.spec_from_file_location(
    "_tools_test_discord_payloads", Path(__file__).parent / "test_discord.py"
)
assert _discord_spec is not None and _discord_spec.loader is not None
_payloads = importlib.util.module_from_spec(_discord_spec)
_discord_spec.loader.exec_module(_payloads)
_search_spec = importlib.util.spec_from_file_location(
    "_tools_test_discord_search_payloads", Path(__file__).parent / "test_discord_search.py"
)
assert _search_spec is not None and _search_spec.loader is not None
_search = importlib.util.module_from_spec(_search_spec)
_search_spec.loader.exec_module(_search)

_SECRET = b"a" * 32
_GUILD = "111"
_CALLER = "42"
_SEALED = "222"
_OTHER = "333"
_VIEW_SEND_AND_HISTORY = (1 << 10) | (1 << 11) | (1 << 16)
_FERNET_KEY = Fernet.generate_key().decode("ascii")

_CONVERSATIONS_INFO = re.compile(r"https://slack\.com/api/conversations\.info.*")
_CONVERSATIONS_HISTORY = re.compile(r"https://slack\.com/api/conversations\.history.*")
_USERS_INFO = re.compile(r"https://slack\.com/api/users\.info.*")
_POST_KEY = ("POST", URL("https://slack.com/api/chat.postMessage"))


def _app(sessionmaker: async_sessionmaker[AsyncSession]) -> ASGIApp:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(
                jwt_secret=SecretStr(_SECRET.decode()), public_url=HttpUrl("https://x/mcp")
            ),
            discord=DiscordSettings(bot_token=SecretStr("test-bot-token")),
            slack=SlackSettings(
                signing_secret=SecretStr("x" * 32), app_token=SecretStr("xapp-test")
            ),
            crypto=CryptoSettings(keys=(SecretStr(_FERNET_KEY),)),
            _env_file=None,  # type: ignore[call-arg]  # isolate from repo .env
        ),
        sessionmaker=sessionmaker,
    )


async def _caller(
    db_session: AsyncSession,
    *,
    platform: str,
    workspace_id: str,
    user_id: str,
    policy: TenantAccessPolicy,
    origin_channel_id: str | None = None,
    origin_thread_id: str = "thread-of-the-turn",
    chat_agent: str | None = "agent_x",
) -> tuple[str, str | None]:
    """Seed the tenant, the caller and the policy; return (token, origin_context_id).

    The token is a real signed ordinary-chat token executing as ``chat_agent``
    (None: an unbound token); the origin's responder is always ``agent_x``.
    """
    tenant = await make_tenant(db_session, platform=platform, workspace_id=workspace_id)  # pyright: ignore[reportArgumentType]
    principal = await make_platform_principal(
        db_session, platform=platform, external_id=user_id, tenant=tenant
    )
    await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    origin_id: str | None = None
    if origin_channel_id is not None:
        now = dt.datetime.now(dt.UTC)
        origin = await create_origin(
            db_session,
            tenant_id=tenant.id,
            account_id=principal.account_id,
            platform=platform,
            parent_channel_id=origin_channel_id,
            thread_id=origin_thread_id,
            responder_ma_agent_id="agent_x",
            responder_name="daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=now + dt.timedelta(minutes=10),
            now=now,
        )
        origin_id = str(origin.id)
    if platform == "slack":
        fernet = build_multifernet((_FERNET_KEY,))
        await upsert_slack_bot_token(
            db_session, team_id=workspace_id, encrypted_token=encrypt_token(fernet, "xoxb-x")
        )
    await db_session.commit()
    token = mint_jwt(
        account_id=principal.account_id,
        secret=_SECRET,
        now=dt.datetime.now(dt.UTC),
        chat_agent_id=(
            derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=chat_agent)
            if chat_agent is not None
            else None
        ),
    )
    return token, origin_id


async def _call(
    app: ASGIApp, token: str, tool: str, arguments: dict[str, object]
) -> dict[str, Any]:
    result = await call_mcp_tool(app, token=token, name=tool, arguments=arguments)
    return result.get("result", result)  # type: ignore[return-value]


def _text(payload: dict[str, Any]) -> str:
    return " ".join(
        str(item.get("text", "")) for item in payload.get("content", []) if isinstance(item, dict)
    )


# --- Discord -----------------------------------------------------------------


def _discord_handler(history_hits: list[str]) -> Any:
    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _payloads._guild_payload(guild_id=_GUILD)  # pyright: ignore[reportPrivateUsage]
        if route.path == "/guilds/{guild_id}/roles":
            return [_payloads._everyone_role(_GUILD, _VIEW_SEND_AND_HISTORY)]  # pyright: ignore[reportPrivateUsage]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _payloads._member_payload(_CALLER)  # pyright: ignore[reportPrivateUsage]
        if route.method == "GET" and route.path == "/channels/{channel_id}":
            return _payloads._text_channel_payload(  # pyright: ignore[reportPrivateUsage]
                channel_id=str(route.channel_id), guild_id=_GUILD
            )
        if route.path == "/guilds/{guild_id}/threads/active":
            return {
                "threads": [
                    _payloads._thread_payload(thread_id="901", parent_id=_OTHER),  # pyright: ignore[reportPrivateUsage]
                    {
                        **_payloads._thread_payload(thread_id="902", parent_id=_OTHER),  # pyright: ignore[reportPrivateUsage]
                        "name": "sealed acquisition target",
                    },
                ],
                "members": [],
            }
        if route.path == "/channels/{channel_id}/threads/archived/public":
            return {"threads": [], "members": [], "has_more": False}
        if route.method == "GET" and route.path == "/channels/{channel_id}/messages":
            history_hits.append(str(route.channel_id))
            return []
        if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
            raise AssertionError("a protected channel must receive no post")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    return handler


@pytest.mark.asyncio
async def test_discord_read_of_a_sealed_channel_from_outside_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token, origin_id = await _caller(
        db_session,
        platform="discord",
        workspace_id=_GUILD,
        user_id=_CALLER,
        policy=TenantAccessPolicy(sealed_channel_ids=(_SEALED,)),
        origin_channel_id=_OTHER,
    )
    history_hits: list[str] = []
    patch_discord_http(monkeypatch, _discord_handler(history_hits))
    app = _app(sessionmaker)

    for arguments in (
        {"channel_id": _SEALED},
        {"channel_id": _SEALED, "origin_context_id": origin_id},
        {"channel_id": _SEALED, "origin_context_id": str(uuid.uuid4())},
    ):
        payload = await _call(app, token, "read_channel", arguments)
        assert payload.get("isError") and "limited to turns inside it" in _text(payload), (
            f"a sealed channel read from outside must be refused; got {payload!r}"
        )
    assert history_hits == [], "a refused read must never fetch the sealed channel's history"


@pytest.mark.asyncio
async def test_discord_read_of_a_sealed_channel_from_a_turn_inside_it_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token, origin_id = await _caller(
        db_session,
        platform="discord",
        workspace_id=_GUILD,
        user_id=_CALLER,
        policy=TenantAccessPolicy(sealed_channel_ids=(_SEALED,)),
        origin_channel_id=_SEALED,
    )
    history_hits: list[str] = []
    patch_discord_http(monkeypatch, _discord_handler(history_hits))

    payload = await _call(
        _app(sessionmaker),
        token,
        "read_channel",
        {"channel_id": _SEALED, "origin_context_id": origin_id},
    )

    assert not payload.get("isError"), f"a turn inside the sealed channel may read it: {payload!r}"
    assert history_hits == [_SEALED], "the read must reach the sealed channel's history"


@pytest.mark.asyncio
async def test_discord_send_into_a_protected_channel_is_refused_through_the_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token, _ = await _caller(
        db_session,
        platform="discord",
        workspace_id=_GUILD,
        user_id=_CALLER,
        policy=TenantAccessPolicy(protected_channel_ids=(_OTHER,)),
    )
    patch_discord_http(monkeypatch, _discord_handler([]))

    payload = await _call(
        _app(sessionmaker), token, "send_message", {"channel_id": _OTHER, "content": "hi"}
    )

    assert payload.get("isError") and "writers to none" in _text(payload), f"got {payload!r}"


# --- Slack -------------------------------------------------------------------


def _mock_public_channel(m: aioresponses, channel_id: str) -> None:
    m.get(  # pyright: ignore[reportUnknownMemberType]
        _CONVERSATIONS_INFO,
        payload={"ok": True, "channel": {"id": channel_id, "name": "c", "is_private": False}},
        repeat=True,
    )
    m.get(  # pyright: ignore[reportUnknownMemberType]
        _USERS_INFO,
        payload={"ok": True, "user": {"id": "U_CALLER", "is_restricted": False}},
        repeat=True,
    )


@pytest.mark.asyncio
async def test_slack_read_of_a_sealed_channel_from_outside_is_refused(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token, origin_id = await _caller(
        db_session,
        platform="slack",
        workspace_id="T_TEST",
        user_id="U_CALLER",
        policy=TenantAccessPolicy(sealed_channel_ids=("C_SEALED",)),
        origin_channel_id="C_OTHER",
    )
    app = _app(sessionmaker)
    with aioresponses(passthrough=["http://127.0.0.1", "http://testserver"]) as m:
        _mock_public_channel(m, "C_SEALED")
        for tool, arguments in (
            ("read_channel", {"channel_id": "C_SEALED", "origin_context_id": origin_id}),
            ("read_thread", {"thread_id": "C_SEALED:1700000000.000100"}),
            ("get_message", {"channel_id": "C_SEALED", "message_id": "1700000000.000100"}),
        ):
            payload = await _call(app, token, tool, arguments)
            assert payload.get("isError") and "limited to turns inside it" in _text(payload), (
                f"{tool} of a sealed channel from outside must be refused; got {payload!r}"
            )
            assert "connect" not in _text(payload).lower(), "no connect hint on a sealed refusal"
        assert not any(url == _CONVERSATIONS_HISTORY for _, url in m.requests), (
            "a refused read must never fetch the sealed channel's history"
        )


@pytest.mark.asyncio
async def test_slack_send_into_a_protected_channel_is_refused_through_the_dispatcher(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token, _ = await _caller(
        db_session,
        platform="slack",
        workspace_id="T_TEST",
        user_id="U_CALLER",
        policy=TenantAccessPolicy(protected_channel_ids=("C_CLIENT",)),
    )
    with aioresponses(passthrough=["http://127.0.0.1", "http://testserver"]) as m:
        _mock_public_channel(m, "C_CLIENT")
        payload = await _call(
            _app(sessionmaker), token, "send_message", {"channel_id": "C_CLIENT", "content": "hi"}
        )
        assert _POST_KEY not in m.requests, "a protected channel must receive no post"
    assert payload.get("isError") and "writers to none" in _text(payload), f"got {payload!r}"


@pytest.mark.parametrize(
    ("channel_id", "parent_channel_id", "origin", "expected"),
    [
        ("open", None, frozenset[str](), True),
        ("vault", None, frozenset[str](), False),
        ("thread", "vault", frozenset[str](), False),
        ("thread", "vault", frozenset({"vault"}), True),
        ("vault", None, frozenset({"elsewhere"}), False),
    ],
    ids=["unsealed", "sealed", "thread-under-sealed", "thread-from-inside", "foreign-origin"],
)
def test_read_policy_allows_a_sealed_channel_only_from_inside_it(
    channel_id: str, parent_channel_id: str | None, origin: frozenset[str], expected: bool
) -> None:
    read_policy = ChannelReadPolicy(
        policy=TenantAccessPolicy(sealed_channel_ids=("vault",)), origin_channel_ids=origin
    )
    assert read_policy.allows(channel_id, parent_channel_id) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chat_agent", ["agent_other", None], ids=["foreign-responder", "unbound-token"]
)
async def test_discord_sealed_origin_is_refused_to_a_token_of_another_responder(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    chat_agent: str | None,
) -> None:
    """The origin belongs to agent_x. A chat token executing as another agent,
    or bound to none, can't borrow it to read the sealed channel."""
    token, origin_id = await _caller(
        db_session,
        platform="discord",
        workspace_id=_GUILD,
        user_id=_CALLER,
        policy=TenantAccessPolicy(sealed_channel_ids=(_SEALED,)),
        origin_channel_id=_SEALED,
        chat_agent=chat_agent,
    )
    history_hits: list[str] = []
    patch_discord_http(monkeypatch, _discord_handler(history_hits))

    payload = await _call(
        _app(sessionmaker),
        token,
        "read_channel",
        {"channel_id": _SEALED, "origin_context_id": origin_id},
    )

    assert payload.get("isError") and "limited to turns inside it" in _text(payload), (
        f"got {payload!r}"
    )
    assert history_hits == [], "a borrowed origin must never reach the sealed history"


@pytest.mark.asyncio
async def test_discord_list_threads_withholds_an_individually_sealed_thread(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token, _ = await _caller(
        db_session,
        platform="discord",
        workspace_id=_GUILD,
        user_id=_CALLER,
        policy=TenantAccessPolicy(sealed_channel_ids=("902",)),
    )
    patch_discord_http(monkeypatch, _discord_handler([]))

    payload = await _call(_app(sessionmaker), token, "list_threads", {"channel_id": _OTHER})

    assert not payload.get("isError"), f"got {payload!r}"
    assert "901" in _text(payload), "the open thread is still listed"
    assert "902" not in _text(payload) and "sealed acquisition target" not in _text(payload), (
        "a sealed thread's id and name must not reach an outside turn"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin_thread", "allowed"),
    [(None, False), ("1700000000.000100", True)],
    ids=["outside", "inside-the-thread"],
)
async def test_slack_read_thread_honours_a_seal_on_the_thread_itself(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    origin_thread: str | None,
    allowed: bool,
) -> None:
    """A Slack thread is sealed as channel_id:thread_ts; its open channel doesn't
    open it."""
    token, origin_id = await _caller(
        db_session,
        platform="slack",
        workspace_id="T_TEST",
        user_id="U_CALLER",
        policy=TenantAccessPolicy(sealed_channel_ids=("C_OPEN:1700000000.000100",)),
        origin_channel_id="C_OPEN" if origin_thread else None,
        origin_thread_id=origin_thread or "unused",
    )
    arguments: dict[str, object] = {"thread_id": "C_OPEN:1700000000.000100"}
    if origin_id is not None:
        arguments["origin_context_id"] = origin_id
    with aioresponses(passthrough=["http://127.0.0.1", "http://testserver"]) as m:
        _mock_public_channel(m, "C_OPEN")
        m.get(  # pyright: ignore[reportUnknownMemberType]
            re.compile(r"https://slack\.com/api/conversations\.replies.*"),
            payload={"ok": True, "messages": [{"ts": "1700000000.000100", "text": "secret"}]},
            repeat=True,
        )
        payload = await _call(_app(sessionmaker), token, "read_thread", arguments)

    if allowed:
        assert not payload.get("isError") and "secret" in _text(payload), f"got {payload!r}"
    else:
        assert payload.get("isError") and "limited to turns inside it" in _text(payload), (
            f"got {payload!r}"
        )
        assert "secret" not in _text(payload)


@pytest.mark.asyncio
async def test_slack_get_message_withholds_a_reply_inside_a_sealed_thread(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    token, _ = await _caller(
        db_session,
        platform="slack",
        workspace_id="T_TEST",
        user_id="U_CALLER",
        policy=TenantAccessPolicy(sealed_channel_ids=("C_OPEN:1700000000.000100",)),
    )
    with aioresponses(passthrough=["http://127.0.0.1", "http://testserver"]) as m:
        _mock_public_channel(m, "C_OPEN")
        m.get(  # pyright: ignore[reportUnknownMemberType]
            _CONVERSATIONS_HISTORY, payload={"ok": True, "messages": []}, repeat=True
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            re.compile(r"https://slack\.com/api/conversations\.replies.*"),
            payload={
                "ok": True,
                "messages": [
                    {
                        "ts": "1700000000.000200",
                        "thread_ts": "1700000000.000100",
                        "text": "secret reply",
                    }
                ],
            },
            repeat=True,
        )
        payload = await _call(
            _app(sessionmaker),
            token,
            "get_message",
            {"channel_id": "C_OPEN", "message_id": "1700000000.000200"},
        )

    assert payload.get("isError") and "limited to turns inside it" in _text(payload), (
        f"got {payload!r}"
    )
    assert "secret reply" not in _text(payload)


# --- Discord search -----------------------------------------------------------


def _discord_search_handler(search_hits: list[int]) -> Any:
    """Channel 222 (sealed in the tests), 333 open, thread 999 under 222."""

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _payloads._guild_payload(guild_id=_GUILD)  # pyright: ignore[reportPrivateUsage]
        if route.path == "/guilds/{guild_id}/roles":
            return [_payloads._everyone_role(_GUILD, _VIEW_SEND_AND_HISTORY)]  # pyright: ignore[reportPrivateUsage]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _payloads._member_payload(_CALLER)  # pyright: ignore[reportPrivateUsage]
        if route.path == "/guilds/{guild_id}/channels":
            return [
                _payloads._text_channel_payload(channel_id=c, guild_id=_GUILD)  # pyright: ignore[reportPrivateUsage]
                for c in (_SEALED, _OTHER)
            ]
        if route.path == "/channels/{channel_id}":
            if str(route.channel_id) == "999":
                return _search._thread_payload(thread_id="999", parent_id=_SEALED)  # pyright: ignore[reportPrivateUsage]
            return _payloads._text_channel_payload(  # pyright: ignore[reportPrivateUsage]
                channel_id=str(route.channel_id), guild_id=_GUILD
            )
        if route.path == "/guilds/{guild_id}/messages/search":
            search_hits.append(1)
            return _search._search_response(  # pyright: ignore[reportPrivateUsage]
                messages=[
                    [
                        _search._search_hit_payload(
                            message_id="1", channel_id=_SEALED, content="in sealed"
                        )
                    ],  # pyright: ignore[reportPrivateUsage]
                    [
                        _search._search_hit_payload(
                            message_id="2", channel_id="999", content="in sealed thread"
                        )
                    ],  # pyright: ignore[reportPrivateUsage]
                    [
                        _search._search_hit_payload(
                            message_id="3", channel_id=_OTHER, content="in open"
                        )
                    ],  # pyright: ignore[reportPrivateUsage]
                ],
                total_results=3,
                threads=[_search._thread_payload(thread_id="999", parent_id=_SEALED)],  # pyright: ignore[reportPrivateUsage]
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin_channel", "expected"),
    [(None, ["in open"]), (_SEALED, ["in sealed", "in sealed thread", "in open"])],
    ids=["outside", "inside"],
)
async def test_discord_unscoped_search_withholds_sealed_hits_outside(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    origin_channel: str | None,
    expected: list[str],
) -> None:
    token, origin_id = await _caller(
        db_session,
        platform="discord",
        workspace_id=_GUILD,
        user_id=_CALLER,
        policy=TenantAccessPolicy(sealed_channel_ids=(_SEALED,)),
        origin_channel_id=origin_channel,
    )
    patch_discord_http(monkeypatch, _discord_search_handler([]))
    arguments: dict[str, object] = {"content": "x"}
    if origin_id is not None:
        arguments["origin_context_id"] = origin_id

    result = await call_mcp_tool(
        _app(sessionmaker), token=token, name="search_messages", arguments=arguments
    )

    payload = result.get("result", result)
    structured = payload.get("structuredContent") or {}  # type: ignore[union-attr]
    structured = structured.get("result", structured)
    assert [r["content"] for r in structured["rows"]] == expected, f"got {payload!r}"
    assert structured["total_results"] == len(expected), "an unscoped total counts only shown rows"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [_SEALED, "999"], ids=["sealed-channel", "thread-under-sealed"])
async def test_discord_scoped_search_into_a_sealed_channel_is_refused_before_searching(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    scope: str,
) -> None:
    token, _ = await _caller(
        db_session,
        platform="discord",
        workspace_id=_GUILD,
        user_id=_CALLER,
        policy=TenantAccessPolicy(sealed_channel_ids=(_SEALED,)),
    )
    search_hits: list[int] = []
    patch_discord_http(monkeypatch, _discord_search_handler(search_hits))

    payload = await _call(
        _app(sessionmaker), token, "search_messages", {"content": "x", "channel_ids": [scope]}
    )

    assert payload.get("isError") and "limited to turns inside it" in _text(payload), (
        f"got {payload!r}"
    )
    assert search_hits == [], "the search route must not be hit"


# --- unreadable policy ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("read_channel", {"channel_id": _OTHER}),
        ("read_thread", {"thread_id": "999"}),
        ("get_message", {"channel_id": _OTHER, "message_id": "1"}),
        ("list_threads", {"channel_id": _OTHER}),
        ("search_messages", {"content": "x"}),
    ],
)
async def test_every_read_is_refused_when_the_policy_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tool: str,
    arguments: dict[str, object],
) -> None:
    token, _ = await _caller(
        db_session,
        platform="discord",
        workspace_id=_GUILD,
        user_id=_CALLER,
        policy=TenantAccessPolicy(),
    )
    await db_session.execute(
        text("UPDATE tenant_access_policies SET policy = 'null'::jsonb"),
    )
    await db_session.commit()
    discord_calls: list[str] = []

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        discord_calls.append(route.path)
        raise AssertionError("an unreadable policy must refuse before any Discord call")

    patch_discord_http(monkeypatch, handler)

    payload = await _call(_app(sessionmaker), token, tool, arguments)

    assert payload.get("isError") and "could not be read" in _text(payload), f"got {payload!r}"
    assert discord_calls == []


def _thread_created_notice(*, message_id: str, thread_id: str) -> dict[str, Any]:
    """Discord's THREAD_CREATED system message: its content is the thread's name."""
    return {
        **_payloads._message_payload(  # pyright: ignore[reportPrivateUsage]
            message_id=message_id, channel_id=_OTHER, content="sealed acquisition target"
        ),
        "type": 18,
        "message_reference": {"channel_id": thread_id, "guild_id": _GUILD},
    }


def _history_handler(fetched: list[str]) -> Any:
    open_message = _payloads._message_payload(  # pyright: ignore[reportPrivateUsage]
        message_id="4999", channel_id=_OTHER, content="open talk"
    )
    notice = _thread_created_notice(message_id="5000", thread_id="902")
    base = _discord_handler([])

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.method == "GET" and route.path == "/channels/{channel_id}/messages":
            fetched.append("history")
            return [notice, open_message]
        if route.method == "GET" and route.path == "/channels/{channel_id}/messages/{message_id}":
            fetched.append("message")
            return notice
        return await base(route, kwargs)

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("inside", [False, True], ids=["outside", "inside-the-thread"])
async def test_discord_thread_created_notices_do_not_name_a_sealed_thread(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    inside: bool,
) -> None:
    """The parent's THREAD_CREATED notice carries the sealed thread's name; it is
    withheld from read_channel and refused by get_message outside the thread."""
    token, origin_id = await _caller(
        db_session,
        platform="discord",
        workspace_id=_GUILD,
        user_id=_CALLER,
        policy=TenantAccessPolicy(sealed_channel_ids=("902",)),
        origin_channel_id=_OTHER if inside else None,
        origin_thread_id="902",
    )
    fetched: list[str] = []
    patch_discord_http(monkeypatch, _history_handler(fetched))
    app = _app(sessionmaker)
    extra: dict[str, object] = {"origin_context_id": origin_id} if origin_id else {}

    history = await _call(app, token, "read_channel", {"channel_id": _OTHER, **extra})
    message = await _call(
        app, token, "get_message", {"channel_id": _OTHER, "message_id": "5000", **extra}
    )

    assert not history.get("isError"), f"got {history!r}"
    assert "open talk" in _text(history)
    if inside:
        assert "sealed acquisition target" in _text(history)
        assert not message.get("isError") and "sealed acquisition target" in _text(message)
    else:
        assert "sealed acquisition target" not in _text(history), "the name is withheld"
        assert message.get("isError") and "limited to turns inside it" in _text(message), (
            f"got {message!r}"
        )
        assert "sealed acquisition target" not in _text(message)


@pytest.mark.asyncio
async def test_discord_scoped_search_counts_only_what_it_shows_once_anything_is_sealed(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A scope on an open parent could count hits in a thread sealed under it."""
    token, _ = await _caller(
        db_session,
        platform="discord",
        workspace_id=_GUILD,
        user_id=_CALLER,
        policy=TenantAccessPolicy(sealed_channel_ids=("999",)),
    )
    patch_discord_http(monkeypatch, _discord_search_handler([]))

    result = await call_mcp_tool(
        _app(sessionmaker),
        token=token,
        name="search_messages",
        arguments={"content": "x", "channel_ids": [_OTHER]},
    )

    payload = result.get("result", result)
    structured = payload.get("structuredContent") or {}  # type: ignore[union-attr]
    structured = structured.get("result", structured)
    shown = len(structured["rows"])
    assert structured["total_results"] == shown, "the total must not count withheld hits"
    assert not structured.get("hint"), "no hint that hidden matches exist"


def _search_with(hits: list[dict[str, Any]], *, total: int) -> Any:
    """The search handler, answering with these hits (all in open channel 333)."""
    base = _discord_search_handler([])

    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}/messages/search":
            return _search._search_response(  # pyright: ignore[reportPrivateUsage]
                messages=[[h] for h in hits], total_results=total
            )
        return await base(route, kwargs)

    return handler


def _search_notice(*, message_id: str, thread_id: str | None) -> dict[str, Any]:
    notice = {
        **_search._search_hit_payload(  # pyright: ignore[reportPrivateUsage]
            message_id=message_id, channel_id=_OTHER, content="sealed acquisition target"
        ),
        "type": 18,
    }
    if thread_id is not None:
        notice["message_reference"] = {"channel_id": thread_id, "guild_id": _GUILD}
    return notice


async def _search_rows(
    sessionmaker: async_sessionmaker[AsyncSession], token: str, arguments: dict[str, object]
) -> dict[str, Any]:
    result = await call_mcp_tool(
        _app(sessionmaker), token=token, name="search_messages", arguments=arguments
    )
    payload = result.get("result", result)
    structured = payload.get("structuredContent") or {}  # type: ignore[union-attr]
    return structured.get("result", structured)  # type: ignore[no-any-return]


@pytest.mark.asyncio
@pytest.mark.parametrize("inside", [False, True], ids=["outside", "inside-the-thread"])
async def test_discord_search_withholds_thread_created_notices_for_sealed_threads(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    inside: bool,
) -> None:
    """A visible hit in the open parent comes first (so the parent's view is
    cached), then the notice naming sealed thread 902, then a notice naming no
    thread. Only the ordinary hit, plus the 902 notice from inside, may show."""
    token, origin_id = await _caller(
        db_session,
        platform="discord",
        workspace_id=_GUILD,
        user_id=_CALLER,
        policy=TenantAccessPolicy(sealed_channel_ids=("902",)),
        origin_channel_id=_OTHER if inside else None,
        origin_thread_id="902",
    )
    ordinary = _search._search_hit_payload(  # pyright: ignore[reportPrivateUsage]
        message_id="1", channel_id=_OTHER, content="open talk"
    )
    hits = [
        ordinary,
        _search_notice(message_id="2", thread_id="902"),
        _search_notice(message_id="3", thread_id=None),
    ]
    patch_discord_http(monkeypatch, _search_with(hits, total=3))
    arguments: dict[str, object] = {"content": "x"}
    if origin_id is not None:
        arguments["origin_context_id"] = origin_id

    structured = await _search_rows(sessionmaker, token, arguments)

    ids = [r["id"] for r in structured["rows"]]
    assert ids == (["1", "2"] if inside else ["1"]), f"got {structured!r}"


@pytest.mark.asyncio
async def test_discord_search_hint_does_not_depend_on_the_raw_total_once_sealed(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The reviewer's probe: same visible hit, limit 1, raw totals 1 and 2 (the
    second match possibly sealed). The two answers must be identical."""
    token, _ = await _caller(
        db_session,
        platform="discord",
        workspace_id=_GUILD,
        user_id=_CALLER,
        policy=TenantAccessPolicy(sealed_channel_ids=("902",)),
    )
    hit = _search._search_hit_payload(  # pyright: ignore[reportPrivateUsage]
        message_id="1", channel_id=_OTHER, content="open talk"
    )
    answers = []
    for total in (1, 2):
        patch_discord_http(monkeypatch, _search_with([hit], total=total))
        structured = await _search_rows(
            sessionmaker, token, {"content": "x", "channel_ids": [_OTHER], "limit": 1}
        )
        answers.append((structured["total_results"], structured.get("hint")))

    assert answers[0] == answers[1], f"the raw total leaks through: {answers}"
    assert answers[0][0] == 1
