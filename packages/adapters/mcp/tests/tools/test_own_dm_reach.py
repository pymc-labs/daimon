"""Own-DM reach and its per-turn latch, through the registered Discord read tools.

Real Postgres, the real JWT and identity path and the real turn origin; only Discord's
HTTP is faked at the transport. The room rule is on: the room facts make "222" and "333"
narrow, "444" is open, and the caller is Discord user "42".
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import uuid
from pathlib import Path
from typing import Any

import discord
import discord.http
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.server import create_mcp_app
from daimon.adapters.mcp.tools._channel_policy import (
    caller_reach_latched,
    require_channel_writable,
    require_dm_recipient_allowed,
    require_schedulable,
)
from daimon.adapters.mcp.tools.routines import (
    _update_routine_impl as update_routine_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.audience_facts import RoomFacts
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_chat_mcp_token, mint_panel_mcp_token
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import access_policy as policy_store
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.audience_facts import StoredRoom, publish_facts
from daimon.core.stores.domain import Role
from daimon.core.stores.turn_origins import create_origin, get_live_origin, mark_reach_read
from daimon.testing.asgi import call_mcp_tool
from daimon.testing.factories import make_platform_principal, make_tenant
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import ASGIApp

_dir = Path(__file__).parent


def _load(name: str, file: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, _dir / file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


patch_discord_http = _load("_reach_conftest", "conftest.py").patch_discord_http
_payloads = _load("_reach_discord_payloads", "test_discord.py")
_search = _load("_reach_search_payloads", "test_discord_search.py")

_SECRET = b"a" * 32
_GUILD = "111"
_CALLER = "42"
_LIMITED = "222"
_LIMITED_TOO = "333"
_OPEN = "444"
_VIEW = 1 << 10
_SEND = 1 << 11
_HISTORY = 1 << 16
_ALL = _VIEW | _SEND | _HISTORY
_LIMITED_ROOMS = (_LIMITED, _LIMITED_TOO)
_ROOM_NAMES = ("sealed-sales", "sealed-ops", "open-room")


@pytest.fixture(autouse=True)
def _room_rule_on(monkeypatch: pytest.MonkeyPatch) -> None:
    async def on(*_: object, **__: object) -> str:
        return "on"

    monkeypatch.setattr(policy_store, "audience_mode", on)


class _Room:
    """What the fake Discord says about the caller, changeable between two calls."""

    def __init__(self) -> None:
        self.deny: dict[str, int] = {}
        self.private_member = True
        self.fetched: list[str] = []

    def overwrites(self, channel_id: str) -> list[dict[str, Any]]:
        bits = self.deny.get(channel_id, 0)
        return [{"id": _CALLER, "type": 1, "allow": "0", "deny": str(bits)}] if bits else []

    def channel(self, channel_id: str) -> dict[str, Any]:
        return _payloads._text_channel_payload(  # pyright: ignore[reportPrivateUsage]
            channel_id=channel_id,
            guild_id=_GUILD,
            permission_overwrites=self.overwrites(channel_id),
        )

    async def handler(self, route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return {
                **_payloads._guild_payload(guild_id=_GUILD),  # pyright: ignore[reportPrivateUsage]
                "roles": [_payloads._everyone_role(_GUILD, _ALL)],  # pyright: ignore[reportPrivateUsage]
            }
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _payloads._member_payload(_CALLER)  # pyright: ignore[reportPrivateUsage]
        if route.path == "/guilds/{guild_id}/channels":
            return [self.channel(c) for c in (*_LIMITED_ROOMS, _OPEN)]
        if route.method == "GET" and route.path == "/channels/{channel_id}":
            channel_id = str(route.channel_id)
            if channel_id in ("901", "902"):
                kind = 12 if channel_id == "902" else 11
                return _search._thread_payload(  # pyright: ignore[reportPrivateUsage]
                    thread_id=channel_id, parent_id=_LIMITED, thread_type=kind
                )
            return self.channel(channel_id)
        if route.path == "/channels/{channel_id}/thread-members/{user_id}":
            if not self.private_member:
                raise discord.NotFound(_NotFound(), {"message": "Unknown Member"})
            return {
                "id": "902",
                "user_id": _CALLER,
                "join_timestamp": "2026-01-01T00:00:00+00:00",
                "flags": 0,
            }
        if route.path == "/guilds/{guild_id}/threads/active":
            return {
                "threads": [
                    _search._thread_payload(  # pyright: ignore[reportPrivateUsage]
                        thread_id="901", parent_id=_LIMITED
                    ),
                    _search._thread_payload(  # pyright: ignore[reportPrivateUsage]
                        thread_id="902", parent_id=_LIMITED, thread_type=12
                    ),
                ],
                "members": [],
            }
        if route.path == "/channels/{channel_id}/threads/archived/public":
            return {"threads": [], "members": [], "has_more": False}
        if route.method == "GET" and route.path == "/channels/{channel_id}/messages":
            self.fetched.append(str(route.channel_id))
            return [
                _payloads._message_payload(  # pyright: ignore[reportPrivateUsage]
                    message_id="1001", channel_id=str(route.channel_id), content="sealed words"
                )
            ]
        if route.method == "GET" and route.path == "/channels/{channel_id}/messages/{message_id}":
            self.fetched.append(str(route.channel_id))
            return _payloads._message_payload(  # pyright: ignore[reportPrivateUsage]
                message_id="1001", channel_id=str(route.channel_id), content="sealed words"
            )
        if route.path == "/guilds/{guild_id}/messages/search":
            return _search._search_response(  # pyright: ignore[reportPrivateUsage]
                messages=[
                    [_hit("1", _LIMITED, "limited, readable")],
                    [_hit("2", _LIMITED_TOO, "limited, caller blocked")],
                    [_hit("3", _OPEN, "open")],
                    [_hit("4", "901", "thread of the limited room")],
                ],
                total_results=4,
                threads=[
                    _search._thread_payload(thread_id="901", parent_id=_LIMITED)  # pyright: ignore[reportPrivateUsage]
                ],
            )
        raise AssertionError(f"unexpected route {route.method} {route.path}")


class _NotFound:
    status = 404
    reason = "Not Found"


def _hit(message_id: str, channel_id: str, content: str) -> dict[str, Any]:
    return _search._search_hit_payload(  # pyright: ignore[reportPrivateUsage]
        message_id=message_id, channel_id=channel_id, content=content
    )


def _app(sessionmaker: async_sessionmaker[AsyncSession]) -> ASGIApp:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(
                jwt_secret=SecretStr(_SECRET.decode()), public_url=HttpUrl("https://x/mcp")
            ),
            discord=DiscordSettings(bot_token=SecretStr("test-bot-token")),
            crypto=CryptoSettings(keys=(SecretStr("a" * 43 + "="),)),
            _env_file=None,  # type: ignore[call-arg]  # isolate from repo .env
        ),
        sessionmaker=sessionmaker,
    )


class _Caller:
    def __init__(self, *, token: str, origin_id: str, tenant_id: uuid.UUID, account_id: uuid.UUID):
        self.token = token
        self.origin_id = origin_id
        self.tenant_id = tenant_id
        self.account_id = account_id


async def _caller(
    db: AsyncSession,
    *,
    reach: bool,
    channel_turn: bool = False,
    attended: bool = True,
    token_kind: str = "chat",
    session: str = "test-session",
    policy: TenantAccessPolicy | None = None,
    narrow: bool = True,
) -> _Caller:
    """A Discord DM turn (or a turn in the limited room's neighbour) with its signed token.

    `narrow` is what the room facts say of the two limited rooms; `policy` is what an admin
    set by hand, nothing by default.
    """
    tenant = await make_tenant(db, platform="discord", workspace_id=_GUILD)  # pyright: ignore[reportArgumentType]
    principal = await make_platform_principal(
        db, platform="discord", external_id=_CALLER, tenant=tenant
    )
    await set_access_policy(
        db,
        tenant_id=tenant.id,
        policy=policy or TenantAccessPolicy(),
    )
    now = dt.datetime.now(dt.UTC)
    await publish_facts(
        db,
        tenant_id=tenant.id,
        rooms={
            **{r: StoredRoom(RoomFacts(contained=False, narrow=narrow)) for r in _LIMITED_ROOMS},
            _OPEN: StoredRoom(RoomFacts(contained=False, narrow=False)),
        },
        now=now,
        fresh_until=now + dt.timedelta(minutes=10),
    )
    scope = _OPEN if channel_turn else f"dm:{uuid.uuid4()}"
    origin = await create_origin(
        db,
        tenant_id=tenant.id,
        account_id=principal.account_id,
        platform="discord",
        parent_channel_id=scope,
        thread_id=scope,
        responder_ma_agent_id="agent_x",
        responder_name="daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        expires_at=now + dt.timedelta(minutes=10),
        now=now,
        attended=attended,
        chat_session_id="test-session",
        own_dm_reach=reach,
    )
    if token_kind == "panel":
        token = await mint_panel_mcp_token(
            db, account_id=principal.account_id, secret=_SECRET, now=now
        )
    else:
        token, _jti, _expiry = await mint_chat_mcp_token(
            db,
            account_id=principal.account_id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_x"),
            secret=_SECRET,
            chat_session_id=session,
            now=now,
        )
    await db.commit()
    return _Caller(
        token=token, origin_id=str(origin.id), tenant_id=tenant.id, account_id=principal.account_id
    )


async def _call(
    app: ASGIApp, who: _Caller, tool: str, arguments: dict[str, object]
) -> dict[str, Any]:
    result = await call_mcp_tool(
        app,
        token=who.token,
        name=tool,
        arguments={**arguments, "origin_context_id": who.origin_id}
        if tool != "send_message"
        else arguments,
    )
    return result.get("result", result)  # type: ignore[return-value]


def _text(payload: dict[str, Any]) -> str:
    return " ".join(
        str(item.get("text", "")) for item in payload.get("content", []) if isinstance(item, dict)
    )


async def _latched(sessionmaker: async_sessionmaker[AsyncSession], who: _Caller) -> bool:
    async with sessionmaker() as db:
        row = await get_live_origin(
            db,
            origin_id=uuid.UUID(who.origin_id),
            tenant_id=who.tenant_id,
            platform="discord",
            now=dt.datetime.now(dt.UTC),
        )
    assert row is not None
    return row.reach_read


_READS: list[tuple[str, dict[str, object]]] = [
    ("read_channel", {"channel_id": _LIMITED}),
    ("get_message", {"channel_id": _LIMITED, "message_id": "1001"}),
    ("read_thread", {"thread_id": "901"}),
    ("list_threads", {"channel_id": _LIMITED}),
]
_READ_IDS = ["read_channel", "get_message", "read_thread", "list_threads"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arguments"), _READS, ids=_READ_IDS)
async def test_a_dm_turn_with_reach_reads_a_limited_room_its_person_can_read(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tool: str,
    arguments: dict[str, object],
) -> None:
    who = await _caller(db_session, reach=True)
    room = _Room()
    patch_discord_http(monkeypatch, room.handler)

    payload = await _call(_app(sessionmaker), who, tool, arguments)

    assert not payload.get("isError"), f"got {payload!r}"
    assert await _latched(sessionmaker, who), "a read through reach latches the turn"


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arguments"), _READS, ids=_READ_IDS)
@pytest.mark.parametrize(
    ("missing", "message"),
    [(_VIEW, "view_channel"), (_HISTORY, "read_message_history")],
    ids=["no-view", "no-history"],
)
async def test_reach_needs_the_callers_own_live_view_and_history(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tool: str,
    arguments: dict[str, object],
    missing: int,
    message: str,
) -> None:
    who = await _caller(db_session, reach=True)
    room = _Room()
    room.deny[_LIMITED] = missing
    patch_discord_http(monkeypatch, room.handler)

    payload = await _call(_app(sessionmaker), who, tool, arguments)

    assert payload.get("isError"), f"got {payload!r}"
    assert "sealed words" not in _text(payload)
    assert not await _latched(sessionmaker, who), "a refused read latches nothing"
    if tool != "list_threads":
        assert message in _text(payload) or "limited to conversations" in _text(payload)
        assert room.fetched == [], "no message may be fetched for a caller who cannot read"


@pytest.mark.asyncio
async def test_a_private_thread_needs_the_callers_own_membership(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    who = await _caller(db_session, reach=True)
    room = _Room()
    room.private_member = False
    patch_discord_http(monkeypatch, room.handler)
    app = _app(sessionmaker)

    refused = await _call(app, who, "read_thread", {"thread_id": "902"})
    listed = await _call(app, who, "list_threads", {"channel_id": _LIMITED})

    assert refused.get("isError") and room.fetched == []
    assert "901" in _text(listed) and "902" not in _text(listed), "the private thread is hidden"

    room.private_member = True
    allowed = await _call(app, who, "read_thread", {"thread_id": "902"})
    assert not allowed.get("isError"), f"got {allowed!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["channel-turn", "no-capability", "panel-token", "other-session"],
)
@pytest.mark.parametrize(
    ("tool", "arguments"), [_READS[0], _READS[3]], ids=["read_channel", "list_threads"]
)
async def test_every_other_caller_is_refused_as_before(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tool: str,
    arguments: dict[str, object],
    case: str,
) -> None:
    who = await _caller(
        db_session,
        # A turn in a channel never holds the capability; a token with no turn behind it, or
        # another session's, is refused even next to an origin that does.
        reach=case in ("panel-token", "other-session"),
        channel_turn=case == "channel-turn",
        token_kind="panel" if case == "panel-token" else "chat",
        session="another-session" if case == "other-session" else "test-session",
    )
    room = _Room()
    patch_discord_http(monkeypatch, room.handler)

    payload = await _call(_app(sessionmaker), who, tool, arguments)

    assert payload.get("isError") and "limited to conversations inside it" in _text(payload), (
        f"got {payload!r}"
    )
    assert room.fetched == []
    assert not await _latched(sessionmaker, who)


@pytest.mark.asyncio
async def test_a_policy_with_no_limited_room_never_uses_reach(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    who = await _caller(db_session, reach=True, narrow=False)
    patch_discord_http(monkeypatch, _Room().handler)

    payload = await _call(_app(sessionmaker), who, "read_channel", {"channel_id": _LIMITED})

    assert not payload.get("isError")
    assert not await _latched(sessionmaker, who), "an open room needs no reach and no latch"


@pytest.mark.asyncio
@pytest.mark.parametrize("narrow", [True, False], ids=["narrow-too", "sealed-only"])
async def test_a_room_an_admin_sealed_by_hand_keeps_its_rule(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    narrow: bool,
) -> None:
    sealed = TenantAccessPolicy(sealed_channel_ids=_LIMITED_ROOMS)
    who = await _caller(db_session, reach=True, policy=sealed, narrow=narrow)
    room = _Room()
    patch_discord_http(monkeypatch, room.handler)

    payload = await _call(_app(sessionmaker), who, "read_channel", {"channel_id": _LIMITED})

    assert payload.get("isError") and "limited to conversations inside it" in _text(payload)
    assert room.fetched == []
    assert not await _latched(sessionmaker, who)


@pytest.mark.asyncio
async def test_search_shows_the_hits_the_caller_can_read_and_hides_the_rest(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    who = await _caller(db_session, reach=True)
    room = _Room()
    room.deny[_LIMITED_TOO] = _VIEW
    patch_discord_http(monkeypatch, room.handler)

    payload = await _call(_app(sessionmaker), who, "search_messages", {"content": "x"})

    text = _text(payload)
    assert not payload.get("isError"), f"got {payload!r}"
    for shown in ("limited, readable", "open", "thread of the limited room"):
        assert shown in text
    assert "caller blocked" not in text
    assert await _latched(sessionmaker, who)


@pytest.mark.asyncio
async def test_search_from_a_turn_without_reach_still_hides_every_limited_hit(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    who = await _caller(db_session, reach=False)
    patch_discord_http(monkeypatch, _Room().handler)

    payload = await _call(_app(sessionmaker), who, "search_messages", {"content": "x"})

    text = _text(payload)
    assert "open" in text and "limited" not in text
    assert not await _latched(sessionmaker, who)


@pytest.mark.asyncio
async def test_reach_follows_the_callers_access_between_two_calls(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    who = await _caller(db_session, reach=True)
    room = _Room()
    patch_discord_http(monkeypatch, room.handler)
    app = _app(sessionmaker)

    first = await _call(app, who, "read_channel", {"channel_id": _LIMITED})
    room.deny[_LIMITED] = _VIEW  # the person is blocked after the first read
    room.fetched.clear()
    second = await _call(app, who, "read_channel", {"channel_id": _LIMITED})

    assert not first.get("isError")
    assert second.get("isError") and room.fetched == [], "access is read again on every call"


# --- The latch -------------------------------------------------------------------


async def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession], who: _Caller
) -> tuple[McpRuntime, AuthIdentity]:
    from unittest.mock import MagicMock

    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )
    auth = AuthIdentity(
        account_id=who.account_id,
        tenant_id=who.tenant_id,
        role=Role.USER,
        platform="discord",
        platform_user_id=_CALLER,
        chat_agent_id=derive_agent_uuid(tenant_id=who.tenant_id, ma_agent_id="agent_x"),
        chat_session_id="test-session",
    )
    return runtime, auth


async def _latch(db: AsyncSession, who: _Caller) -> None:
    await mark_reach_read(db, origin_id=uuid.UUID(who.origin_id))
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("attended", [True, False], ids=["interactive-dm", "owner-dm-routine"])
async def test_a_turn_that_read_a_limited_room_posts_and_sends_only_to_its_own_person(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    attended: bool,
) -> None:
    who = await _caller(db_session, reach=True, attended=attended)
    runtime, auth = await _runtime(sessionmaker, who)
    # Before the read, the turn posts and sends as it always did.
    await require_channel_writable(runtime, auth, channel_id=_OPEN)
    await require_dm_recipient_allowed(runtime, auth, recipient_id="777")
    assert not await caller_reach_latched(runtime, auth)

    await _latch(db_session, who)

    assert await caller_reach_latched(runtime, auth)
    with pytest.raises(ToolError, match="only into the caller's own direct messages"):
        await require_channel_writable(runtime, auth, channel_id=_OPEN)
    with pytest.raises(ToolError, match="only into the caller's own direct messages"):
        await require_channel_writable(runtime, auth, channel_id="555", parent_channel_id=_OPEN)
    with pytest.raises(ToolError, match="only into the caller's own direct messages"):
        await require_dm_recipient_allowed(runtime, auth, recipient_id="777")
    await require_dm_recipient_allowed(runtime, auth, recipient_id=_CALLER)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["create_routine", "create_timer"])
async def test_a_turn_that_read_a_limited_room_saves_no_routine_and_no_timer(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    tool: str,
) -> None:
    # The routine tool reads a DM turn's source conversation before its view; a turn with
    # no such row stands in for it here, since the latch is read from the origin alone.
    who = await _caller(db_session, reach=True, channel_turn=tool == "create_routine")
    runtime, auth = await _runtime(sessionmaker, who)
    await require_schedulable(runtime, auth)  # before the read: nothing is refused here
    await _latch(db_session, who)
    arguments: dict[str, dict[str, object]] = {
        "create_routine": {
            "agent_name": "daimon",
            "cron_expr": "0 9 * * *",
            "timezone": "UTC",
            "trigger_message": "post what you read",
        },
        "create_timer": {"fire_at": "in 5 minutes", "note": "post what you read"},
    }

    payload = await _call(_app(sessionmaker), who, tool, arguments[tool])

    assert payload.get("isError") and "sets up no routine or timer" in _text(payload), (
        f"got {payload!r}"
    )
    # A routine's words are changed by the same rule.
    with pytest.raises(ToolError, match="sets up no routine or timer"):
        await update_routine_impl(
            runtime, auth, routine_id=uuid.uuid4(), trigger_message="post what you read"
        )


@pytest.mark.asyncio
async def test_the_latch_reaches_the_send_tool_without_naming_the_origin(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    who = await _caller(db_session, reach=True)
    room = _Room()
    patch_discord_http(monkeypatch, room.handler)
    app = _app(sessionmaker)
    read = await _call(app, who, "read_channel", {"channel_id": _LIMITED})
    assert not read.get("isError")

    sent = await _call(
        app,
        who,
        "send_message",
        {"channel_id": _OPEN, "content": "see sealed", "plan": {"places": 1}},
    )

    assert sent.get("isError") and "only into the caller's own direct messages" in _text(sent), (
        f"got {sent!r}"
    )


@pytest.mark.asyncio
async def test_a_helper_of_a_latched_turn_is_latched_too(
    db_session: AsyncSession, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    who = await _caller(db_session, reach=True)
    runtime, auth = await _runtime(sessionmaker, who)
    now = dt.datetime.now(dt.UTC)
    await create_origin(
        db_session,
        tenant_id=who.tenant_id,
        account_id=who.account_id,
        platform="discord",
        parent_channel_id=_OPEN,
        thread_id=_OPEN,
        responder_ma_agent_id="agent_x",
        responder_name="helper",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        expires_at=now + dt.timedelta(minutes=10),
        now=now,
        chat_session_id="helper-session",
        helper_of=uuid.UUID(who.origin_id),
    )
    helper_auth = AuthIdentity(
        **{**auth.__dict__, "chat_session_id": "helper-session"}  # pyright: ignore[reportArgumentType]
    )
    await db_session.commit()
    assert not await caller_reach_latched(runtime, helper_auth)

    await _latch(db_session, who)

    assert await caller_reach_latched(runtime, helper_auth)
    with pytest.raises(ToolError, match="only into the caller's own direct messages"):
        await require_channel_writable(runtime, helper_auth, channel_id=_OPEN)
