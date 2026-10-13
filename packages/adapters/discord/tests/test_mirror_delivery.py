"""Discord's mirror poster, driven through the core outbox poll."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.mirror_delivery import (
    AgentNames,
    ma_agent_names,
    make_discord_mirror_poster,
)
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.config import MirrorsSettings
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mirror_delivery import poll_mirror_deliveries_once, record_conversation_event
from daimon.core.mirror_events import ActorRef, ConversationEventV1, SourceRef
from daimon.core.stores import mirrors as store
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import MirrorRow
from daimon.testing import ma_agent, ma_model_config
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = 424242
_CHANNEL = 555
_AGENT = uuid.uuid4()
_NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _channel(*, guild_id: int = _GUILD) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = _CHANNEL
    channel.guild = SimpleNamespace(id=guild_id)
    channel.category_id = None
    channel.thread = _thread(777)
    header = MagicMock()
    header.create_thread = AsyncMock(return_value=channel.thread)
    channel.send = AsyncMock(return_value=header)
    return channel


def _thread(thread_id: int, *, parent_id: int = _CHANNEL, locked: bool = False) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.parent_id, thread.locked, thread.send = (
        thread_id,
        parent_id,
        locked,
        AsyncMock(),
    )
    thread.guild = SimpleNamespace(id=_GUILD)
    thread.parent = None
    return thread


def _http_error(kind: type[discord.HTTPException], status: int) -> discord.HTTPException:
    return kind(MagicMock(status=status, reason="error"), "error")


async def _setup(
    db_session: AsyncSession,
    policy: TenantAccessPolicy | None = None,
    *,
    dest_kind: Literal["channel", "thread"] = "channel",
    dest_id: int = _CHANNEL,
    ma_agent_id: str | None = None,
) -> MirrorRow:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD))
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    mirror = await store.upsert_mirror(
        db_session,
        tenant_id=tenant.id,
        name="qa",
        source_agent_id=(
            _AGENT
            if ma_agent_id is None
            else derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=ma_agent_id)
        ),
        source_agent_name="helper",
        source_agent_names=("helper",),
        dest_platform="discord",
        dest_kind=dest_kind,
        dest_id=str(dest_id),
        consent_note=None,
        max_answer_chars=None,
        account_id=None,
        now=_NOW,
    )
    await db_session.commit()
    return mirror


async def _ask_and_poll(
    sm: async_sessionmaker[AsyncSession],
    mirror: MirrorRow,
    channel: MagicMock,
    *,
    seal_ids: tuple[str, ...] = (),
    fetch: Callable[[int], Awaitable[object]] | None = None,
    names: AgentNames | None = None,
) -> None:
    event = ConversationEventV1(
        id=uuid.uuid4(),
        surface="agent_chat",
        occurred_at=_NOW,
        tenant_id=mirror.tenant_id,
        agent_id=mirror.source_agent_id,
        source=SourceRef(channel_id="300", seal_ids=seal_ids, session_key="sesn_abc123"),
        actor=ActorRef(display_name="Ada"),
        question="Ping @everyone?",
        answer="Pong.",
    )
    await record_conversation_event(sm, event, now=_NOW)

    async def fetch_default(channel_id: int) -> object:
        return channel if channel_id == _CHANNEL else channel.thread

    async def helper(_mirror: MirrorRow) -> tuple[str, ...] | None:
        return ("helper",)

    poster = make_discord_mirror_poster(
        sm,
        fetch_channel=fetch or fetch_default,
        agent_names=names or helper,
        max_answer_chars=3000,
    )
    await poll_mirror_deliveries_once(
        sm, platform="discord", post=poster, settings=MirrorsSettings(), now=_NOW
    )


async def test_a_session_opens_one_thread_and_posts_without_mentions(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mirror = await _setup(db_session)
    channel = _channel()

    await _ask_and_poll(db_session_factory, mirror, channel)
    await _ask_and_poll(db_session_factory, mirror, channel)

    assert channel.send.await_count == 1, "the session's thread is opened once, then reused"
    channel.send.return_value.create_thread.assert_awaited_once_with(name="helper · abc123")
    sent = channel.thread.send.await_args_list
    assert [call.kwargs["content"].split("\n")[1] for call in sent] == [
        "> Ping @everyone?",
        "> Pong.",
    ] * 2, sent
    every_call = [*sent, *channel.send.await_args_list]
    assert all(call.kwargs["allowed_mentions"].to_dict() == {"parse": []} for call in every_call), (
        "a mirror never pings anyone"
    )
    assert all(call.kwargs["suppress_embeds"] for call in every_call), "nor unfurls links"


async def test_a_channel_of_another_server_pauses_the_mirror(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mirror = await _setup(db_session)
    channel = _channel(guild_id=1)

    await _ask_and_poll(db_session_factory, mirror, channel)

    channel.send.assert_not_awaited()
    paused = await store.get_mirror(db_session, mirror.id)
    assert paused is not None and paused.paused_reason == "destination_unavailable", paused


async def test_a_rule_letting_nobody_post_pauses_the_mirror(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    policy = TenantAccessPolicy(channel_rules={str(_CHANNEL): ChannelRule(writers="none")})
    mirror = await _setup(db_session, policy)
    channel = _channel()

    await _ask_and_poll(db_session_factory, mirror, channel)

    channel.send.assert_not_awaited()
    paused = await store.get_mirror(db_session, mirror.id)
    assert paused is not None and paused.paused_reason == "writers_none", paused


async def test_a_sealed_session_is_skipped_and_the_mirror_goes_on(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mirror = await _setup(db_session)
    channel = _channel()

    await _ask_and_poll(db_session_factory, mirror, channel, seal_ids=("300",))

    channel.send.assert_not_awaited()
    after = await store.get_mirror(db_session, mirror.id)
    assert after is not None and after.status == "active", "one session's refusal pauses nothing"


async def _status(db_session: AsyncSession, mirror: MirrorRow) -> tuple[str, str | None]:
    after = await store.get_mirror(db_session, mirror.id)
    assert after is not None, "the mirror exists"
    return after.status, after.paused_reason


async def test_a_passing_discord_error_skips_the_post_without_pausing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mirror = await _setup(db_session)

    async def fetch(_channel_id: int) -> object:
        raise _http_error(discord.HTTPException, 503)

    await _ask_and_poll(db_session_factory, mirror, _channel(), fetch=fetch)

    assert await _status(db_session, mirror) == ("active", None), "a 5xx pauses nothing"


async def test_a_bot_that_may_not_post_pauses_the_mirror(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mirror = await _setup(db_session)
    channel = _channel()
    channel.send.side_effect = _http_error(discord.Forbidden, 403)

    await _ask_and_poll(db_session_factory, mirror, channel)

    assert await _status(db_session, mirror) == ("paused", "bot_cannot_post")


@pytest.mark.parametrize(
    "gone", ["deleted", "forbidden", "foreign_parent", "locked", "send_refused"]
)
async def test_a_session_thread_the_bot_cannot_use_is_replaced(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], gone: str
) -> None:
    mirror = await _setup(db_session)
    channel = _channel()
    await _ask_and_poll(db_session_factory, mirror, channel)
    channel.thread = _thread(
        888, parent_id=1 if gone == "foreign_parent" else _CHANNEL, locked=gone == "locked"
    )
    channel.send.return_value.create_thread.return_value = _thread(999)
    if gone == "send_refused":
        channel.thread.send.side_effect = _http_error(discord.Forbidden, 403)

    async def fetch(channel_id: int) -> object:
        if channel_id == _CHANNEL:
            return channel
        if gone in ("deleted", "forbidden"):
            kind = discord.NotFound if gone == "deleted" else discord.Forbidden
            raise _http_error(kind, 404 if gone == "deleted" else 403)
        return channel.thread

    await _ask_and_poll(db_session_factory, mirror, channel, fetch=fetch)

    replacement = channel.send.return_value.create_thread.return_value
    assert channel.send.await_count == 2, "a new thread is opened for the session"
    assert replacement.send.await_count == 2, "the question and answer go to the new thread"
    assert await _status(db_session, mirror) == ("active", None), "the mirror goes on"


async def test_a_thread_destination_takes_posts_straight_in(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    mirror = await _setup(db_session, dest_kind="thread", dest_id=777)
    channel = _channel()

    await _ask_and_poll(db_session_factory, mirror, channel)

    channel.send.assert_not_awaited()
    assert channel.thread.send.await_count == 2, "the question and answer, no header"


@pytest.mark.parametrize(
    ("agents", "reason"),
    [([], "agent_missing"), (["home-bot"], "own_agents_only")],
    ids=["gone", "renamed_into_a_home"],
)
async def test_the_agents_current_names_decide_at_delivery(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    agents: list[str],
    reason: str,
) -> None:
    policy = TenantAccessPolicy(
        channel_rules={"100": ChannelRule(readers="own", writers="own")},
        agent_rules={"home-bot": AgentRule(runs_in=("100",))},
    )
    mirror = await _setup(db_session, policy, ma_agent_id="ag_helper")
    channel = _channel()
    router = MARouter()
    listed = [
        ma_agent(
            id="ag_helper",
            name=name,
            model=ma_model_config("claude-sonnet-4-6", speed="standard"),
            metadata={MA_METADATA_KEY_TENANT: str(mirror.tenant_id)},
        ).model_dump(mode="json")
        for name in agents
    ]
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response(listed))
    names = ma_agent_names(build_fake_anthropic(router.dispatch))

    await _ask_and_poll(db_session_factory, mirror, channel, names=names)

    channel.send.assert_not_awaited()
    assert await _status(db_session, mirror) == ("paused", reason), "the stored name is not trusted"
