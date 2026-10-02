"""Discord tidy tools: an agent edits and deletes only what it posted itself.

Messages are posted through the real ``send_message``/``create_thread``
impls so the ownership record is written the way production writes it, then
tidied through the tidy impls. Discord is faked at the HTTP transport
(``patch_discord_http``), so discord.py's own models parse every payload.
"""

from __future__ import annotations

import hashlib
import importlib.util
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import discord
import discord.http
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import SealedChannelError
from daimon.adapters.mcp.tools.discord._send import (
    _send_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._threads import (
    _create_thread_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._tidy import (
    _archive_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _delete_message_impl,  # pyright: ignore[reportPrivateUsage]
    _delete_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _edit_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
    SupportSettings,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.agent_posts import get_post, record_post
from daimon.core.stores.domain import Role
from daimon.core.stores.security_audit import SecurityAuditRow, list_events
from daimon.core.stores.turn_origins import create_origin
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_conftest_path = Path(__file__).parent / "conftest.py"
_spec = importlib.util.spec_from_file_location("_tools_conftest", _conftest_path)
assert _spec is not None and _spec.loader is not None
_tools_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tools_conftest)
patch_discord_http = _tools_conftest.patch_discord_http

_GUILD = "111"
_CHANNEL = "222"
_OTHER_CHANNEL = "333"
_THREAD = "444"
_CALLER = "42"
_BOT = "1"  # the fake static_login reports the bot as user "1"
_AGENT = "ag_acme"
_OTHER_AGENT = "ag_other"
_ALL_PERMS = (1 << 10) | (1 << 11) | (1 << 35) | (1 << 38)


# ---------------------------------------------------------------------------
# A small fake of the Discord REST surface the tools touch.
# ---------------------------------------------------------------------------


def _user(user_id: str, *, bot: bool) -> dict[str, Any]:
    return {
        "id": user_id,
        "username": f"user{user_id}",
        "discriminator": "0001",
        "global_name": None,
        "avatar": None,
        "bot": bot,
        "flags": 0,
    }


@dataclass
class _FakeDiscord:
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    next_id: int = 1_400_000_000_000_000_000
    thread_archived: bool = False
    thread_deleted: bool = False

    def add(
        self,
        channel_id: str,
        *,
        author_id: str = _BOT,
        bot: bool = True,
        content: str = "x",
        message_type: int = 0,
    ) -> str:
        self.next_id += 1
        message_id = str(self.next_id)
        self.messages[message_id] = {
            "id": message_id,
            "channel_id": channel_id,
            "author": _user(author_id, bot=bot),
            "content": content,
            "timestamp": "2026-10-01T00:00:00+00:00",
            "edited_timestamp": None,
            "tts": False,
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "attachments": [],
            "embeds": [],
            "type": message_type,
            "pinned": False,
            "flags": 0,
        }
        return message_id

    def channel(self, channel_id: str) -> dict[str, Any]:
        if channel_id == _THREAD:
            return {
                "id": _THREAD,
                "parent_id": _CHANNEL,
                "owner_id": _BOT,
                "name": "tidy-thread",
                "type": 11,
                "guild_id": _GUILD,
                "message_count": 1,
                "member_count": 1,
                "thread_metadata": {
                    "archived": self.thread_archived,
                    "locked": False,
                    "auto_archive_duration": 1440,
                    "archive_timestamp": "2026-10-01T00:00:00+00:00",
                },
                "last_message_id": None,
                "rate_limit_per_user": 0,
            }
        return {
            "id": channel_id,
            "type": 0,
            "guild_id": _GUILD,
            "name": f"chan-{channel_id}",
            "position": 0,
            "permission_overwrites": [],
            "nsfw": False,
            "rate_limit_per_user": 0,
            "parent_id": None,
        }

    async def handle(self, route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        method, path = route.method, route.path
        tail = route.url.rsplit("/", 1)[-1]
        self.calls.append((method, route.url))
        if path == "/guilds/{guild_id}":
            return {
                "id": _GUILD,
                "name": "g",
                "owner_id": "9",
                "afk_timeout": 0,
                "verification_level": 0,
                "default_message_notifications": 0,
                "explicit_content_filter": 0,
                "roles": [],
                "emojis": [],
                "features": [],
                "mfa_level": 0,
                "system_channel_flags": 0,
                "premium_tier": 0,
                "preferred_locale": "en-US",
                "nsfw_level": 0,
                "premium_progress_bar_enabled": False,
                "stickers": [],
            }
        if path == "/guilds/{guild_id}/roles":
            return [
                {
                    "id": _GUILD,
                    "name": "@everyone",
                    "permissions": str(_ALL_PERMS),
                    "position": 0,
                    "color": 0,
                    "hoist": False,
                    "managed": False,
                    "mentionable": False,
                    "flags": 0,
                }
            ]
        if path == "/guilds/{guild_id}/members/{member_id}":
            return {
                "user": _user(_CALLER, bot=False),
                "roles": [],
                "joined_at": "2024-01-01T00:00:00+00:00",
                "deaf": False,
                "mute": False,
                "flags": 0,
            }
        if path == "/channels/{channel_id}" and method == "GET":
            return self.channel(str(route.channel_id))
        if path == "/channels/{channel_id}" and method == "PATCH":
            self.thread_archived = bool(kwargs["json"].get("archived"))
            return self.channel(str(route.channel_id))
        if path == "/channels/{channel_id}" and method == "DELETE":
            self.thread_deleted = True
            return None
        if path == "/channels/{channel_id}/threads" and method == "POST":
            return self.channel(_THREAD)
        if path == "/channels/{channel_id}/messages" and method == "POST":
            body = kwargs.get("json") or {}
            if not body:
                # discord.py sends multipart (form) when files are attached.
                body = {"content": kwargs["form"][0]["value"]}
            message_id = self.add(str(route.channel_id), content=str(body.get("content", "")))
            return self.messages[message_id]
        if path == "/channels/{channel_id}/messages" and method == "GET":
            in_channel = [
                m for m in self.messages.values() if m["channel_id"] == str(route.channel_id)
            ]
            return sorted(in_channel, key=lambda m: int(m["id"]), reverse=True)
        if path == "/channels/{channel_id}/messages/{message_id}":
            message = self.messages.get(tail)
            if message is None or message["channel_id"] != str(route.channel_id):
                raise discord.NotFound(MagicMock(status=404), {"message": "Unknown Message"})
            if method == "GET":
                return message
            if method == "PATCH":
                message["content"] = kwargs["json"]["content"]
                return message
            if method == "DELETE":
                del self.messages[tail]
                return None
        raise AssertionError(f"unexpected route {method} {path}")

    def did(self, method: str, url_tail: str) -> bool:
        return any(m == method and url.endswith(url_tail) for m, url in self.calls)


# ---------------------------------------------------------------------------
# Tenant, turn and runtime.
# ---------------------------------------------------------------------------


@dataclass
class _World:
    runtime: McpRuntime
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    sessionmaker: async_sessionmaker[AsyncSession]

    async def turn(
        self, *, agent: str = _AGENT, parent: str = _CHANNEL, thread: str = _CHANNEL
    ) -> tuple[AuthIdentity, str]:
        now = datetime.now(UTC)
        async with self.sessionmaker.begin() as s:
            origin = await create_origin(
                s,
                tenant_id=self.tenant_id,
                account_id=self.account_id,
                platform="discord",
                parent_channel_id=parent,
                thread_id=thread,
                responder_ma_agent_id=agent,
                responder_name=agent,
                configuration_target_ma_agent_id=None,
                configuration_target_name=None,
                role=Role.USER,
                expires_at=now + timedelta(hours=1),
                now=now,
            )
        auth = AuthIdentity(
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            role=Role.USER,
            platform="discord",
            external_id=_GUILD,
            platform_user_id=_CALLER,
            chat_agent_id=derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=agent),
        )
        return auth, str(origin.id)

    async def audit(self) -> list[SecurityAuditRow]:
        async with self.sessionmaker() as s:
            return await list_events(s, tenant_id=self.tenant_id)

    async def set_policy(self, policy: TenantAccessPolicy) -> None:
        async with self.sessionmaker.begin() as s:
            await set_access_policy(s, tenant_id=self.tenant_id, policy=policy)


async def _world(
    sessionmaker: async_sessionmaker[AsyncSession], *, escalation_channel_id: str | None = None
) -> _World:
    async with sessionmaker.begin() as s:
        tenant = await make_tenant(s, platform="discord", workspace_id=_GUILD)
        account = await make_account(s, tenant=tenant)
    router = MARouter()
    agents: list[dict[str, Any]] = [
        ma_agent(
            id=agent_id,
            name=agent_id,
            metadata={"daimon_tenant": str(tenant.id), "daimon_name": agent_id},
        ).model_dump(mode="json")
        for agent_id in (_AGENT, _OTHER_AGENT)
    ]
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response(agents))
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://x/y"),  # pyright: ignore[reportArgumentType]
        anthropic=AnthropicSettings(api_key=SecretStr("k")),
        discord=DiscordSettings(bot_token=SecretStr("test-bot-token")),
        support=SupportSettings(escalation_channel_id=escalation_channel_id),
    )
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(router.dispatch),  # type: ignore[arg-type]
        settings=settings,
        deployment_default=DeploymentDefault(),
    )
    return _World(runtime, tenant.id, account.id, sessionmaker)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeDiscord:
    state = _FakeDiscord()
    patch_discord_http(monkeypatch, state.handle)
    return state


async def _post(world: _World, auth: AuthIdentity, *, channel_id: str = _CHANNEL) -> str:
    row = await _send_message_impl(
        world.runtime, auth, channel_id=channel_id, content="first draft"
    )
    return row.id


# ---------------------------------------------------------------------------
# Own messages
# ---------------------------------------------------------------------------


async def test_an_agent_edits_then_deletes_its_own_message_and_each_is_audited_without_text(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    message_id = await _post(world, auth)

    edited = await _edit_message_impl(
        world.runtime,
        auth,
        channel_id=_CHANNEL,
        message_id=message_id,
        content="second draft",
        origin_context_id=origin,
    )
    assert edited.action == "edited", "the edit must report what it did"
    assert fake.messages[message_id]["content"] == "second draft", "discord got the new text"

    deleted = await _delete_message_impl(
        world.runtime, auth, channel_id=_CHANNEL, message_id=message_id, origin_context_id=origin
    )
    assert deleted.action == "deleted", "the delete must report what it did"
    assert message_id not in fake.messages, "discord deleted the message"
    async with committing_sessionmaker() as s:
        post = await get_post(
            s,
            tenant_id=world.tenant_id,
            platform="discord",
            channel_id=_CHANNEL,
            message_id=message_id,
        )
    assert post is None, "a deleted post is no longer anyone's to act on"

    rows = [r for r in await world.audit() if r.target_message_id == message_id]
    assert [(r.tool_name, r.outcome) for r in rows] == [
        ("edit_message", "allowed"),
        ("delete_message", "allowed"),
    ], "each action writes one allowed audit row"
    first, second = rows
    assert first.content_sha256 == hashlib.sha256(b"first draft").hexdigest(), (
        "the edit row records a hash of the text it replaced"
    )
    assert second.content_sha256 == hashlib.sha256(b"second draft").hexdigest(), (
        "the delete row records a hash of the text it removed"
    )
    assert {r.target_channel_id for r in rows} == {_CHANNEL}, "rows carry the channel id"
    assert {r.turn_ref for r in rows} == {f"origin:{origin}"}, "rows carry the turn"
    for row in rows:
        dumped = row.model_dump_json()
        assert "first draft" not in dumped and "second draft" not in dumped, (
            "an audit row never carries message text"
        )


async def test_a_message_in_a_thread_is_addressed_by_the_thread_id(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    message_id = await _post(world, auth, channel_id=_THREAD)

    await _delete_message_impl(
        world.runtime, auth, channel_id=_THREAD, message_id=message_id, origin_context_id=origin
    )
    assert message_id not in fake.messages, "a message in a thread is deleted from that thread"


# ---------------------------------------------------------------------------
# Refusals: not this agent's message
# ---------------------------------------------------------------------------


async def test_a_human_message_is_refused_and_the_refusal_is_audited(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    human = fake.add(_CHANNEL, author_id=_CALLER, bot=False, content="a person wrote this")

    with pytest.raises(ToolError, match="not posted by you"):
        await _delete_message_impl(
            world.runtime, auth, channel_id=_CHANNEL, message_id=human, origin_context_id=origin
        )
    with pytest.raises(ToolError, match="not posted by you"):
        await _edit_message_impl(
            world.runtime,
            auth,
            channel_id=_CHANNEL,
            message_id=human,
            content="rewritten",
            origin_context_id=origin,
        )
    assert human in fake.messages, "the person's message is untouched"
    assert fake.messages[human]["content"] == "a person wrote this", "and unedited"
    rows = [r for r in await world.audit() if r.target_message_id == human]
    assert {(r.outcome, r.reason) for r in rows} == {("denied", "not_posted_by_agent")}, (
        "a refusal is audited with its ids"
    )


async def test_another_agents_message_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    other_auth, _ = await world.turn(agent=_OTHER_AGENT)
    theirs = await _post(world, other_auth)
    auth, origin = await world.turn()

    with pytest.raises(ToolError, match="another agent posted this message"):
        await _delete_message_impl(
            world.runtime, auth, channel_id=_CHANNEL, message_id=theirs, origin_context_id=origin
        )
    assert theirs in fake.messages, "the other agent's message is untouched"


async def test_another_bots_message_is_refused_even_with_a_matching_record(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    other_bot = fake.add(_CHANNEL, author_id="77", bot=True)
    unrecorded_bot = fake.add(_CHANNEL, author_id="78", bot=True)
    assert auth.chat_agent_id is not None
    async with committing_sessionmaker.begin() as s:
        # A record that names this agent for a message daimon did not author.
        await record_post(
            s,
            tenant_id=world.tenant_id,
            platform="discord",
            channel_id=_CHANNEL,
            message_id=other_bot,
            agent_id=auth.chat_agent_id,
        )

    with pytest.raises(ToolError, match="not daimon's own post"):
        await _delete_message_impl(
            world.runtime, auth, channel_id=_CHANNEL, message_id=other_bot, origin_context_id=origin
        )
    with pytest.raises(ToolError, match="not posted by you"):
        await _delete_message_impl(
            world.runtime,
            auth,
            channel_id=_CHANNEL,
            message_id=unrecorded_bot,
            origin_context_id=origin,
        )
    assert {other_bot, unrecorded_bot} <= fake.messages.keys(), "neither bot message is deleted"


async def test_a_record_in_one_channel_does_not_reach_the_same_id_in_another(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    message_id = await _post(world, auth)

    with pytest.raises(ToolError, match="not posted by you"):
        await _delete_message_impl(
            world.runtime,
            auth,
            channel_id=_OTHER_CHANNEL,
            message_id=message_id,
            origin_context_id=origin,
        )


async def test_a_call_with_no_agent_or_no_turn_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    message_id = await _post(world, auth)
    operator = AuthIdentity(
        account_id=world.account_id,
        tenant_id=world.tenant_id,
        role=Role.ADMIN,
        platform="discord",
        external_id=_GUILD,
        platform_user_id=_CALLER,
    )

    with pytest.raises(ToolError, match="only an agent"):
        await _delete_message_impl(
            world.runtime,
            operator,
            channel_id=_CHANNEL,
            message_id=message_id,
            origin_context_id=origin,
        )
    with pytest.raises(ToolError, match="origin_context_id"):
        await _delete_message_impl(
            world.runtime, auth, channel_id=_CHANNEL, message_id=message_id, origin_context_id=None
        )
    assert message_id in fake.messages, "nothing was deleted"


# ---------------------------------------------------------------------------
# Refusals: where the message is
# ---------------------------------------------------------------------------


async def test_a_protected_channel_refuses_tidying_decided_at_call_time(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    message_id = await _post(world, auth)
    # Protected after the post: the decision uses the policy at call time.
    await world.set_policy(TenantAccessPolicy(protected_channel_ids=(_CHANNEL,)))

    with pytest.raises(ToolError, match="protected"):
        await _delete_message_impl(
            world.runtime,
            auth,
            channel_id=_CHANNEL,
            message_id=message_id,
            origin_context_id=origin,
        )
    assert message_id in fake.messages, "nothing was deleted in a protected channel"


async def test_a_pinned_agent_cannot_tidy_outside_its_channels(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    message_id = await _post(world, auth)
    await world.set_policy(TenantAccessPolicy(agent_channel_pins={_AGENT: (_OTHER_CHANNEL,)}))

    with pytest.raises(ToolError, match="pinned to its own channels"):
        await _edit_message_impl(
            world.runtime,
            auth,
            channel_id=_CHANNEL,
            message_id=message_id,
            content="moved",
            origin_context_id=origin,
        )
    assert fake.messages[message_id]["content"] == "first draft", "nothing was edited"


async def test_a_sealed_channel_is_tidied_only_from_a_turn_inside_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    inside_auth, inside = await world.turn()
    message_id = await _post(world, inside_auth)
    await world.set_policy(TenantAccessPolicy(sealed_channel_ids=(_CHANNEL,)))
    outside_auth, outside = await world.turn(parent=_OTHER_CHANNEL, thread=_OTHER_CHANNEL)

    with pytest.raises(SealedChannelError):
        await _delete_message_impl(
            world.runtime,
            outside_auth,
            channel_id=_CHANNEL,
            message_id=message_id,
            origin_context_id=outside,
        )
    assert message_id in fake.messages, "an outside turn changed nothing"

    result = await _delete_message_impl(
        world.runtime,
        inside_auth,
        channel_id=_CHANNEL,
        message_id=message_id,
        origin_context_id=inside,
    )
    assert result.model_dump().keys() == {
        "platform",
        "channel_id",
        "message_id",
        "action",
        "messages_deleted",
    }, "a tidy result carries ids only, never message text"


async def test_the_escalation_channel_and_its_threads_are_never_tidied(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker, escalation_channel_id=_CHANNEL)
    auth, origin = await world.turn()
    in_channel = await _post(world, auth)
    in_thread = await _post(world, auth, channel_id=_THREAD)

    with pytest.raises(ToolError, match="support-escalation channel"):
        await _delete_message_impl(
            world.runtime,
            auth,
            channel_id=_CHANNEL,
            message_id=in_channel,
            origin_context_id=origin,
        )
    with pytest.raises(ToolError, match="support-escalation channel"):
        await _delete_message_impl(
            world.runtime, auth, channel_id=_THREAD, message_id=in_thread, origin_context_id=origin
        )
    assert {in_channel, in_thread} <= fake.messages.keys(), "nothing was deleted"


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


async def test_the_per_turn_limit_stops_the_eleventh_action(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    message_id = await _post(world, auth)
    for i in range(10):
        await _edit_message_impl(
            world.runtime,
            auth,
            channel_id=_CHANNEL,
            message_id=message_id,
            content=f"draft {i}",
            origin_context_id=origin,
        )

    with pytest.raises(ToolError, match="this turn's tidy limit"):
        await _edit_message_impl(
            world.runtime,
            auth,
            channel_id=_CHANNEL,
            message_id=message_id,
            content="one too many",
            origin_context_id=origin,
        )
    assert fake.messages[message_id]["content"] == "draft 9", "the eleventh edit never ran"

    _, next_turn = await world.turn()
    await _edit_message_impl(
        world.runtime,
        auth,
        channel_id=_CHANNEL,
        message_id=message_id,
        content="next turn",
        origin_context_id=next_turn,
    )
    assert fake.messages[message_id]["content"] == "next turn", "a new turn has its own budget"


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


async def test_an_agent_archives_and_deletes_its_own_thread(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    await _create_thread_impl(
        world.runtime, auth, channel_id=_CHANNEL, name="tidy", content="starter"
    )
    # Renaming a thread leaves a system notice authored by the bot.
    fake.add(_THREAD, message_type=4, content="renamed")

    archived = await _archive_thread_impl(
        world.runtime, auth, thread_id=_THREAD, origin_context_id=origin
    )
    assert archived.action == "archived" and fake.thread_archived, "the thread is archived"

    deleted = await _delete_thread_impl(
        world.runtime, auth, thread_id=_THREAD, origin_context_id=origin
    )
    assert deleted.action == "deleted" and fake.thread_deleted, "the thread is deleted"
    assert deleted.messages_deleted == 1, "the starter was the agent's one message in it"
    rows = [r for r in await world.audit() if r.tool_name in {"archive_thread", "delete_thread"}]
    assert [(r.tool_name, r.target_message_id) for r in rows] == [
        ("archive_thread", _THREAD),
        ("delete_thread", _THREAD),
    ], "each thread action is audited with the thread id"


async def test_a_thread_someone_else_wrote_in_is_not_deleted(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    await _create_thread_impl(
        world.runtime, auth, channel_id=_CHANNEL, name="tidy", content="starter"
    )
    fake.add(_THREAD, author_id=_CALLER, bot=False, content="a reply")

    with pytest.raises(ToolError, match="archive_thread instead"):
        await _delete_thread_impl(world.runtime, auth, thread_id=_THREAD, origin_context_id=origin)
    assert not fake.thread_deleted, "a thread with a person's message survives"


async def test_a_thread_this_agent_did_not_open_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: _FakeDiscord
) -> None:
    world = await _world(committing_sessionmaker)
    other_auth, _ = await world.turn(agent=_OTHER_AGENT)
    await _create_thread_impl(
        world.runtime, other_auth, channel_id=_CHANNEL, name="theirs", content="starter"
    )
    auth, origin = await world.turn()

    with pytest.raises(ToolError, match="another agent"):
        await _archive_thread_impl(world.runtime, auth, thread_id=_THREAD, origin_context_id=origin)
    with pytest.raises(ToolError, match="another agent"):
        await _delete_thread_impl(world.runtime, auth, thread_id=_THREAD, origin_context_id=origin)
    assert not fake.thread_archived and not fake.thread_deleted, "the thread is untouched"
