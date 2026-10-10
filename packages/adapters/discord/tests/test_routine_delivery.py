"""Discord's routine result poster (FEAT-085)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import routine_delivery as poster_mod
from daimon.adapters.discord.routine_delivery import make_discord_routine_poster
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_identity import AgentIdentity
from daimon.core.agent_post_identity import fallback_name_prefix
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine, record_result
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_GUILD = 424242


_MEMBER = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False))


def _perms(*, view: bool = True, send: bool = True, manage_threads: bool = False) -> object:
    return SimpleNamespace(
        view_channel=view,
        send_messages=send,
        send_messages_in_threads=send,
        manage_threads=manage_threads,
    )


def _guild(*, guild_id: int = _GUILD, member: object | None = _MEMBER) -> object:
    async def fetch_member(user_id: int) -> object:
        raise discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Member")

    return SimpleNamespace(
        id=guild_id, get_member=lambda user_id: member, fetch_member=fetch_member
    )


def _text_channel(
    *,
    guild_id: int = _GUILD,
    category_id: int | None = None,
    perms: object | None = None,
    member: object | None = _MEMBER,
) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.guild = _guild(guild_id=guild_id, member=member)
    channel.category_id = category_id
    channel.permissions_for = MagicMock(return_value=perms if perms is not None else _perms())
    channel.send = AsyncMock()
    return channel


async def _routine(db_session: AsyncSession, *, destination_id: str = "555") -> RoutineRow:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD))
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="1",
        agent_id="ag",
        agent_name="daimon",
        cron_expr="0 9 * * 1",
        timezone_="UTC",
        trigger_message="go",
        destination_kind="channel",
        destination_id=destination_id,
    )
    await record_result(
        db_session, row.id, tail="All green @everyone.", error=None, delivery="pending"
    )
    await db_session.commit()
    return row.model_copy(
        update={
            "last_result_tail": "All green @everyone.",
            "delivery_payload": "All green @everyone.",
        }
    )


class _Dms:
    """Records DMs opened for the creator; `fail` makes opening raise."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[int, int, str]] = []
        self.fail = fail

    async def open(self, guild_id: int, user_id: int) -> Any:
        if self.fail:
            raise LookupError("not a member")
        dms = self

        class _Dm:
            async def send(self, *, content: str, allowed_mentions: object) -> None:
                dms.sent.append((guild_id, user_id, content))

        return _Dm()


def _poster(
    sm: async_sessionmaker[AsyncSession],
    channels: object | dict[int, object],
    *,
    dms: _Dms | None = None,
    dm_mode: str = "members",
    identity: AgentIdentity | None = None,
) -> Any:
    from daimon.core.config import DirectMessagePolicy

    async def fetch(channel_id: int) -> object:
        found = channels.get(channel_id) if isinstance(channels, dict) else channels
        if found is None or isinstance(found, Exception):
            raise found or discord.NotFound(
                MagicMock(status=404, reason="Not Found"), "Unknown Channel"
            )
        return found

    return make_discord_routine_poster(
        sm,
        fetch_channel=fetch,
        open_dm=(dms or _Dms()).open,
        dm_policy=lambda row: DirectMessagePolicy(mode=dm_mode),  # type: ignore[arg-type]
        client=MagicMock(spec=discord.Client) if identity is not None else None,
        resolve_identity=resolved(identity) if identity is not None else None,
    )


def resolved(identity: AgentIdentity) -> Any:
    async def resolve(row: RoutineRow, guild_id: str) -> AgentIdentity:
        assert guild_id == str(_GUILD), "identity is resolved for the routine's own guild"
        return identity

    return resolve


class _Transport:
    """Stands in for `DiscordPostTransport`, recording how it was built and used."""

    made: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []

    def __init__(self, client: object, channel: object, **kwargs: Any) -> None:
        _Transport.made.append({"channel": channel, **kwargs})

    async def send(self, **kwargs: Any) -> None:
        _Transport.sent.append(kwargs)


def _uncached_thread(
    *, parent_id: int = 444, private: bool = False, creator_in_thread: bool = False
) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.guild = _guild()
    thread.parent = None  # not in the bot's cache
    thread.parent_id = parent_id
    thread.type = (
        discord.ChannelType.private_thread if private else discord.ChannelType.public_thread
    )
    if creator_in_thread:
        thread.fetch_member = AsyncMock(return_value=object())
    else:
        thread.fetch_member = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404, reason="Not Found"), "no")
        )
    thread.send = AsyncMock()
    return thread


async def test_posts_the_tail_with_mentions_disabled(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    channel = _text_channel()

    outcome = await _poster(db_session_factory, channel)(row)

    assert outcome.status == "delivered"
    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"].endswith("All green @everyone.")
    mentions = kwargs["allowed_mentions"]
    assert not mentions.everyone and not mentions.users and not mentions.roles


async def test_with_identity_the_result_posts_as_the_agent_without_from_wording(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(poster_mod, "DiscordPostTransport", _Transport)
    _Transport.made, _Transport.sent = [], []
    row = await _routine(db_session)
    channel = _text_channel()
    identity = AgentIdentity(name="research", avatar_url="https://app/a.png", builtin=False)

    outcome = await _poster(db_session_factory, channel, identity=identity)(row)

    assert outcome.status == "delivered"
    channel.send.assert_not_awaited()
    (made,) = _Transport.made
    assert (made["channel"], made["name"], made["avatar_url"]) == (
        channel,
        "research",
        "https://app/a.png",
    )
    assert made["builtin"] is False and made["identity_enabled"] is True
    (sent,) = _Transport.sent
    assert sent["content"] == "Routine result (0 9 * * 1, UTC):\n\nAll green @everyone."
    assert sent["_prefix_if_fallback"] is True, "a bot fallback still names the agent"
    mentions = sent["allowed_mentions"]
    assert not mentions.everyone and not mentions.users and not mentions.roles


async def test_with_identity_a_long_result_leaves_room_for_the_fallback_label(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(poster_mod, "DiscordPostTransport", _Transport)
    _Transport.made, _Transport.sent = [], []
    row = (await _routine(db_session)).model_copy(update={"delivery_payload": "x" * 2500})
    identity = AgentIdentity(name="research", avatar_url=None, builtin=False)

    await _poster(db_session_factory, _text_channel(), identity=identity)(row)

    assert len(_Transport.sent) > 1
    prefix = fallback_name_prefix("research", "")
    assert all(len(sent["content"]) + len(prefix) <= 2000 for sent in _Transport.sent)
    assert _Transport.sent[0]["_prefix_if_fallback"] is True
    assert all(not sent["_prefix_if_fallback"] for sent in _Transport.sent[1:])
    assert "".join(sent["content"] for sent in _Transport.sent).split("\n\n", 1)[1] == "x" * 2500


async def test_the_built_in_agent_posts_as_the_bot_with_todays_text(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(poster_mod, "DiscordPostTransport", _Transport)
    _Transport.made = []
    row = await _routine(db_session)
    channel = _text_channel()
    identity = AgentIdentity(name="daimon", avatar_url=None, builtin=True)

    await _poster(db_session_factory, channel, identity=identity)(row)

    assert _Transport.made == []
    assert channel.send.await_args.kwargs["content"].startswith(
        "Routine result from daimon (0 9 * * 1, UTC):"
    )


async def test_a_channel_in_a_protected_category_falls_back_to_a_dm(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(protected_category_ids=("77",)),
    )
    await db_session.commit()
    channel = _text_channel(category_id=77)
    dms = _Dms()

    outcome = await _poster(db_session_factory, channel, dms=dms)(row)

    assert (outcome.status, outcome.note) == ("delivered", "dm_fallback:protected_channel")
    channel.send.assert_not_awaited()
    (guild_id, user_id, content) = dms.sent[0]
    assert (guild_id, user_id) == (_GUILD, 1)
    assert "lets nobody write there" in content and content.endswith("All green @everyone.")


async def test_an_uncached_thread_parent_is_resolved_before_category_protection(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Review regression: a thread whose parent was not cached used to skip the
    category check and post into a protected category."""
    row = await _routine(db_session)
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(protected_category_ids=("77",)),
    )
    await db_session.commit()
    thread = _uncached_thread(parent_id=444)
    parent = _text_channel(category_id=77)

    outcome = await _poster(db_session_factory, {555: thread, 444: parent})(row)

    assert outcome.note == "dm_fallback:protected_channel"
    thread.send.assert_not_awaited()


async def test_a_thread_whose_parent_cannot_be_resolved_is_not_posted_to(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    thread = _uncached_thread(parent_id=444)

    outcome = await _poster(db_session_factory, {555: thread})(row)  # parent lookup 404s

    assert outcome.note == "dm_fallback:destination_unavailable"
    thread.send.assert_not_awaited()


async def test_no_dm_when_the_dm_policy_disallows_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    dms = _Dms()
    gone = discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Channel")

    outcome = await _poster(db_session_factory, gone, dms=dms, dm_mode="disabled")(row)

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")
    assert dms.sent == []


async def test_an_archived_tenant_posts_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from datetime import UTC, datetime

    from daimon.core.defaults.provisioning import archive_tenant

    row = await _routine(db_session)
    await archive_tenant(db_session_factory, tenant_id=row.tenant_id, now=datetime.now(UTC))
    channel = _text_channel()

    outcome = await _poster(db_session_factory, channel)(row)

    assert (outcome.status, outcome.note) == ("skipped", "tenant_archived")
    channel.send.assert_not_awaited()


async def test_a_creator_no_longer_allowed_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(invoker_user_ids=("SOMEONE_ELSE",)),
    )
    await db_session.commit()
    channel = _text_channel()

    outcome = await _poster(db_session_factory, channel)(row)

    assert (outcome.status, outcome.note) == ("skipped", "invoker_not_allowed")
    channel.send.assert_not_awaited()


async def test_a_channel_in_another_guild_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    channel = _text_channel(guild_id=999)

    outcome = await _poster(db_session_factory, channel)(row)

    assert (outcome.status, outcome.note) == ("delivered", "dm_fallback:destination_unavailable")
    channel.send.assert_not_awaited()


async def test_a_missing_channel_is_skipped(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    gone = discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Channel")

    outcome = await _poster(db_session_factory, gone, dms=_Dms(fail=True))(row)

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")


async def _unreadable_policy(monkeypatch: Any, tenant_id: object) -> None:
    import daimon.core.routine_delivery as delivery_mod
    from daimon.core.stores.access_policy import AccessPolicyUnreadable

    async def unreadable(*args: object, **kwargs: object) -> TenantAccessPolicy:
        raise AccessPolicyUnreadable(tenant_id=tenant_id)  # type: ignore[arg-type]

    monkeypatch.setattr(delivery_mod, "load_access_policy", unreadable)


@pytest.mark.parametrize(
    ("destination", "policy", "unreadable", "note"),
    [
        # protected + revoked creator
        (
            "protected",
            TenantAccessPolicy(protected_channel_ids=("555",), invoker_user_ids=("OTHER",)),
            False,
            "invoker_not_allowed",
        ),
        # missing channel + revoked creator
        ("missing", TenantAccessPolicy(invoker_user_ids=("OTHER",)), False, "invoker_not_allowed"),
        # missing channel + unreadable policy
        ("missing", None, True, "access_policy_unreadable"),
    ],
)
async def test_a_creator_who_is_not_cleared_gets_nothing_not_even_a_dm(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
    policy: TenantAccessPolicy | None,
    unreadable: bool,
    note: str,
) -> None:
    """Review regression (round 2): protection/unavailability used to be read
    as permission to DM a creator the invoker policy no longer allows."""
    row = await _routine(db_session)
    if policy is not None:
        await set_access_policy(db_session, tenant_id=row.tenant_id, policy=policy)
        await db_session.commit()
    if unreadable:
        await _unreadable_policy(monkeypatch, row.tenant_id)
    channel = _text_channel()
    dms = _Dms()
    channels: dict[int, object] = {555: channel} if destination == "protected" else {}

    outcome = await _poster(db_session_factory, channels, dms=dms)(row)

    assert (outcome.status, outcome.note) == ("skipped", note)
    assert dms.sent == [], "no direct message for an uncleared creator"
    channel.send.assert_not_awaited()


@pytest.mark.parametrize(
    ("channel", "channels_extra"),
    [
        # the creator lost send_messages in the channel
        (lambda: _text_channel(perms=_perms(send=False)), {}),
        # the creator left the guild
        (lambda: _text_channel(member=None), {}),
    ],
)
async def test_a_channel_the_creator_cannot_post_in_gets_their_dm_not_a_post(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    channel: Any,
    channels_extra: dict[int, object],
) -> None:
    """Review regression (round 3): the poster checked the bot's access only."""
    row = await _routine(db_session)
    target = channel()
    dms = _Dms()

    outcome = await _poster(db_session_factory, {555: target, **channels_extra}, dms=dms)(row)

    assert outcome.note == "dm_fallback:creator_cannot_post"
    target.send.assert_not_awaited()


async def test_a_private_thread_the_creator_is_not_in_gets_their_dm_not_a_post(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    thread = _uncached_thread(private=True, creator_in_thread=False)
    parent = _text_channel()

    outcome = await _poster(db_session_factory, {555: thread, 444: parent})(row)

    assert outcome.note == "dm_fallback:creator_cannot_post"
    thread.send.assert_not_awaited()


async def test_a_private_thread_the_creator_is_in_is_posted_to(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    row = await _routine(db_session)
    thread = _uncached_thread(private=True, creator_in_thread=True)
    parent = _text_channel()

    outcome = await _poster(db_session_factory, {555: thread, 444: parent})(row)

    assert outcome.status == "delivered" and outcome.note is None
    thread.send.assert_awaited_once()


@pytest.mark.parametrize(
    "agent", ["daimon", "local"], ids=["outside-agent-posting-in", "own-agent"]
)
async def test_an_isolated_channels_routine_never_leaves_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    agent: str,
) -> None:
    row = (await _routine(db_session)).model_copy(update={"agent_name": agent})
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=row.tenant_id, channel_id="555"),
        tenant_id=row.tenant_id,
        agent_name="local",
        mode="agent",
    )
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(
            sealed_channel_ids=("555",),
            isolated_channel_ids=("555",),
            agent_channel_pins={"local": ("555",)},
        ),
    )
    await db_session.commit()
    gone = discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown Channel")
    dms = _Dms()

    outcome = await _poster(db_session_factory, gone, dms=dms)(row)

    assert (outcome.status, outcome.note) == ("skipped", "destination_unavailable")
    assert dms.sent == [], "the result never goes by DM"


async def test_a_bot_fallback_labels_the_result_with_the_subtext_name_line(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from daimon.adapters.discord import post_transport

    row = await _routine(db_session)
    channel = _text_channel(perms=SimpleNamespace(**vars(_perms()), manage_webhooks=False))
    channel.id = 555
    channel.guild.me = object()  # type: ignore[attr-defined]
    identity = AgentIdentity(name="research", avatar_url=None, builtin=False)
    post_transport._unavailable_until.pop(555, None)  # pyright: ignore[reportPrivateUsage]

    outcome = await _poster(db_session_factory, channel, identity=identity)(row)

    assert outcome.status == "delivered"
    content = channel.send.await_args.kwargs["content"]
    assert content == fallback_name_prefix(
        "research", "Routine result (0 9 * * 1, UTC):\n\nAll green @everyone."
    )
    assert content.startswith("-# research\nRoutine result ("), "the label is #530's subtext line"


@pytest.mark.parametrize("fallback", [False, True])
async def test_long_routine_delivers_every_word_to_channel_or_creator(
    db_session, db_session_factory, fallback
):
    row = await _routine(db_session)
    result = " ".join(f"digestword{i}" for i in range(800)) + " FINAL-COMPLETE"
    row = row.model_copy(update={"delivery_payload": result})
    channel = _text_channel(perms=_perms(send=not fallback))
    dms = _Dms()
    poster = _poster(db_session_factory, channel, dms=dms)
    outcome = await poster(row)
    assert outcome.status == "delivered"
    texts = (
        [item[2] for item in dms.sent]
        if fallback
        else [call.kwargs["content"] for call in channel.send.await_args_list]
    )
    assert len(texts) > 1 and all(len(text) <= 2000 for text in texts)
    delivered = "".join(texts).split("\n\n", 1)[1]
    assert delivered == result, "deliver the beginning and ending, without cutting a word"
    if not fallback:
        assert all(
            call.kwargs["allowed_mentions"].everyone is False
            for call in channel.send.await_args_list
        )
