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
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.agent_posts import list_posts_in
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


async def _routine(
    db_session: AsyncSession,
    *,
    destination_id: str = "555",
    destination_kind="channel",
    channel_id: str | None = None,
) -> RoutineRow:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD))
    from daimon.core.stores.direct_messages import set_dm_enabled

    await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=True)
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="1",
        agent_id="ag",
        agent_name="daimon",
        cron_expr="0 9 * * 1",
        timezone_="UTC",
        trigger_message="go",
        destination_kind=destination_kind,
        destination_id=destination_id,
        channel_id=channel_id,
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

    async def membership(tenant_id, guild_id, user_id):  # type: ignore[no-untyped-def]
        return Membership(is_member=True)

    return make_discord_routine_poster(
        sm,
        fetch_channel=fetch,
        membership_resolver=CachedMembershipResolver(membership),
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

    async def send(self, **kwargs: Any) -> Any:
        _Transport.sent.append(kwargs)
        return SimpleNamespace(id=9000 + len(_Transport.sent))


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
    assert sent["content"] == "Result from the Monday routine at 09:00 UTC:\n\nAll green @everyone."
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
        "Result from daimon's Monday routine at 09:00 UTC:"
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
    assert "posting is blocked there" in content and content.endswith("All green @everyone.")


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
        "research", "Result from the Monday routine at 09:00 UTC:\n\nAll green @everyone."
    )
    assert content.startswith("-# research\nResult from the Monday routine"), (
        "the label is #530's subtext line"
    )


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
    # The result is the last paragraph; above it, the post's one line or the DM's two.
    delivered = "".join(texts).split("\n\n")[-1]
    assert delivered == result, "deliver the beginning and ending, without cutting a word"
    if not fallback:
        assert all(
            call.kwargs["allowed_mentions"].everyone is False
            for call in channel.send.await_args_list
        )


@pytest.mark.parametrize("as_agent", [False, True], ids=["as-bot", "as-agent-webhook"])
async def test_each_delivered_chunk_is_recorded_as_the_routine_agents_post(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    as_agent: bool,
) -> None:
    """Every chunk is recorded, so a reply to any of them (unpingable as a
    webhook) starts a turn, and only the creator or an admin can tidy it."""
    monkeypatch.setattr(poster_mod, "DiscordPostTransport", _Transport)
    _Transport.made, _Transport.sent = [], []
    row = (await _routine(db_session)).model_copy(update={"delivery_payload": "x " * 1500})
    channel = _text_channel()
    channel.id = 555
    channel.send = AsyncMock(side_effect=[SimpleNamespace(id=9001), SimpleNamespace(id=9002)])
    identity = AgentIdentity(name="research", avatar_url=None, builtin=False) if as_agent else None

    outcome = await _poster(db_session_factory, channel, identity=identity)(row)

    assert outcome.status == "delivered"
    async with db_session_factory() as session:
        posts = await list_posts_in(
            session,
            tenant_id=row.tenant_id,
            platform="discord",
            channel_id="555",
            message_ids=["9001", "9002"],
        )
    assert sorted(post.message_id for post in posts) == ["9001", "9002"], "every chunk is recorded"
    agent = derive_agent_uuid(tenant_id=row.tenant_id, ma_agent_id="ag")
    assert all(
        (post.source, post.agent_id, post.requester_platform_user_id, post.kind)
        == ("routine", agent, "1", "message")
        for post in posts
    ), "a routine post names the routine's agent and its creator"


async def test_a_failed_record_does_not_fail_the_delivery(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core import channel_tidy

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(channel_tidy, "record_post", broken)
    row = await _routine(db_session)
    channel = _text_channel()

    outcome = await _poster(db_session_factory, channel)(row)

    assert outcome.status == "delivered", "the result is out; a lost record only loses tidying"
    channel.send.assert_awaited_once()


async def test_role_loss_during_destination_lookup_refuses_delayed_delivery(
    db_session, db_session_factory
):  # type: ignore[no-untyped-def]
    from daimon.core.config import DirectMessagePolicy

    row = await _routine(db_session)
    await set_access_policy(
        db_session, tenant_id=row.tenant_id, policy=TenantAccessPolicy(invoker_role_ids=("123",))
    )
    await db_session.commit()
    facts = [Membership(is_member=True, role_ids=("123",))]
    calls = []

    async def member(tenant_id, guild_id, user_id):  # type: ignore[no-untyped-def]
        calls.append(user_id)
        return facts[0]

    resolver = CachedMembershipResolver(member)
    channel = _text_channel()
    dms = _Dms()

    async def fetch(channel_id):  # type: ignore[no-untyped-def]
        facts[0] = Membership(is_member=True)
        return channel

    poster = make_discord_routine_poster(
        db_session_factory,
        fetch_channel=fetch,
        open_dm=dms.open,
        dm_policy=lambda _: DirectMessagePolicy(),
        membership_resolver=resolver,
    )
    outcome = await poster(row)
    assert outcome.status == "skipped" and outcome.note == "invoker_not_allowed"
    channel.send.assert_not_awaited()
    assert not dms.sent
    assert calls == ["1", "1"], (
        "delivery must bypass the positive cache after the destination await"
    )


@pytest.mark.parametrize("explicit_dm", [False, True])
async def test_routine_dm_does_not_replace_an_active_private_conversation(
    db_session_factory, explicit_dm
):
    from daimon.core.stores.direct_messages import get_conversation
    from daimon.testing.factories import make_account, make_dm_conversation

    async with db_session_factory() as session:
        routine = await _routine(session, destination_id="unavailable")
        active_tenant = await make_tenant(session, workspace_id="700000102")
        account = await make_account(session, tenant=active_tenant)
        active = await make_dm_conversation(
            session,
            tenant=active_tenant,
            account_id=account.id,
            route_key="physical-dm",
            external_user_id="1",
        )
        await session.commit()
    if explicit_dm:
        from daimon.core.stores.direct_messages import set_dm_enabled
        from daimon.core.stores.routines import update_routine

        async with db_session_factory.begin() as session:
            updated = await update_routine(
                session,
                routine.id,
                edited_by=routine.created_by_user_id,
                tenant_id=routine.tenant_id,
                destination_kind="dm",
            )
            await set_dm_enabled(session, tenant_id=routine.tenant_id, enabled=True)
        assert updated is not None
        routine = updated.model_copy(update={"delivery_payload": "A private notification."})
    dms = _Dms()
    outcome = await _poster(db_session_factory, {}, dms=dms)(routine)
    assert outcome.status == "delivered" and dms.sent
    async with db_session_factory() as session:
        after = await get_conversation(
            session, platform="discord", route_key="physical-dm", external_user_id="1"
        )
    assert after == active, "routine output changes neither the active tenant nor private history"


@pytest.mark.parametrize(
    "mode", ["members", "disabled", "tenant_disabled", "blocked", "sealed", "revoked"]
)
async def test_explicit_owner_dm_never_looks_up_channel_or_changes_conversation(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    mode: str,
) -> None:
    from daimon.core.config import DirectMessagePolicy
    from daimon.core.stores.direct_messages import set_dm_enabled
    from daimon.core.stores.routines import update_routine

    row = await _routine(db_session)
    async with db_session_factory.begin() as session:
        updated = await update_routine(
            session,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            destination_kind="dm",
        )
        await set_dm_enabled(session, tenant_id=row.tenant_id, enabled=mode != "tenant_disabled")
    assert updated is not None
    if mode == "sealed":
        from daimon.core.access_policy import ChannelRule

        async with db_session_factory.begin() as session:
            await set_access_policy(
                session,
                tenant_id=row.tenant_id,
                policy=TenantAccessPolicy(channel_rules={"456": ChannelRule(readers="inside")}),
            )
            # The policy may change after creation, before delivery.
            await update_routine(
                session,
                row.id,
                edited_by=row.created_by_user_id,
                tenant_id=row.tenant_id,
                trigger_message="go",
            )
        updated = updated.model_copy(update={"channel_id": "456", "source_channel_id": "456"})
    fetch = AsyncMock(side_effect=AssertionError("a recipient is not a channel"))
    dms = _Dms(fail=mode == "blocked")

    async def membership(tenant_id, guild_id, user_id):
        return Membership(is_member=mode != "revoked")

    poster = make_discord_routine_poster(
        db_session_factory,
        fetch_channel=fetch,
        open_dm=dms.open,
        dm_policy=lambda _: DirectMessagePolicy(
            mode="disabled" if mode == "disabled" else "members"
        ),
        membership_resolver=CachedMembershipResolver(membership),
    )
    outcome = await poster(updated.model_copy(update={"delivery_payload": "private @everyone"}))
    fetch.assert_not_awaited()
    assert outcome.status == ("delivered" if mode == "members" else "skipped")
    assert bool(dms.sent) == (mode == "members")
    if dms.sent:
        assert dms.sent[0][:2] == (_GUILD, 1)
        assert "private @everyone" in dms.sent[0][2]
        assert "Run " in dms.sent[0][2]


@pytest.mark.parametrize(
    "stop", ["partial_failure", "revoked", "destination_changed", "thread_sealed"]
)
async def test_dm_partial_delivery_is_never_retried(db_session_factory, stop):
    from datetime import UTC, datetime

    from daimon.core.config import DirectMessagePolicy
    from daimon.core.routine_delivery import poll_deliveries_once
    from daimon.core.stores.routines import get_routine, update_routine

    async with db_session_factory() as session:
        row = await _routine(
            session,
            destination_kind="thread" if stop == "thread_sealed" else "channel",
            destination_id="789" if stop == "thread_sealed" else "555",
            channel_id="456" if stop == "thread_sealed" else None,
        )
    async with db_session_factory.begin() as session:
        row = await update_routine(
            session,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            destination_kind="dm",
        )
        assert row is not None
        await record_result(session, row.id, tail="private " * 900, error=None, delivery="pending")
    sends = []

    async def member(*args):
        return Membership(is_member=not (stop == "revoked" and sends))

    class Dm:
        async def send(self, *, content, allowed_mentions):
            assert len(content) <= 2000
            assert allowed_mentions.to_dict() == {"parse": []}
            if sends and stop == "partial_failure":
                raise discord.HTTPException(MagicMock(status=500, reason="failed"), "uncertain")
            sends.append(content)
            if stop == "thread_sealed":
                from daimon.core.access_policy import ChannelRule

                async with db_session_factory.begin() as session:
                    await set_access_policy(
                        session,
                        tenant_id=row.tenant_id,
                        policy=TenantAccessPolicy(
                            channel_rules={"789": ChannelRule(readers="inside")}
                        ),
                    )
            if stop == "destination_changed":
                async with db_session_factory.begin() as session:
                    await update_routine(
                        session,
                        row.id,
                        edited_by=row.created_by_user_id,
                        tenant_id=row.tenant_id,
                        destination_kind="channel",
                        destination_id="555",
                    )

    async def open_dm(*args):
        return Dm()

    poster = make_discord_routine_poster(
        db_session_factory,
        fetch_channel=AsyncMock(side_effect=AssertionError("no public fallback")),
        open_dm=open_dm,
        dm_policy=lambda _: DirectMessagePolicy(),
        membership_resolver=CachedMembershipResolver(member),
    )
    now = datetime.now(UTC)
    assert (
        await poll_deliveries_once(db_session_factory, platform="discord", post=poster, now=now)
        == 1
    )
    assert len(sends) == 1
    assert (
        await poll_deliveries_once(db_session_factory, platform="discord", post=poster, now=now)
        == 0
    )
    async with db_session_factory() as session:
        after = await get_routine(session, row.id, tenant_id=row.tenant_id)
    assert after is not None and after.delivery_status == "skipped" and after.delivery_note


async def test_tenant_dm_disablement_also_blocks_routine_fallback(db_session_factory):
    from daimon.core.stores.direct_messages import set_dm_enabled

    async with db_session_factory() as session:
        row = await _routine(session)
    async with db_session_factory.begin() as session:
        await set_dm_enabled(session, tenant_id=row.tenant_id, enabled=False)
    dms = _Dms()
    result = await _poster(db_session_factory, {}, dms=dms)(row)
    assert result.status == "skipped" and "disabled" in result.note
    assert not dms.sent


@pytest.mark.parametrize("agent_state", ["pinned_alias", "unrelated_rule", "missing"])
async def test_dm_delivery_rechecks_all_live_agent_names(db_session_factory, agent_state):
    from daimon.core.access_policy import AgentRule
    from daimon.core.config import DirectMessagePolicy
    from daimon.core.stores.routines import update_routine

    async with db_session_factory() as session:
        row = await _routine(session)
    async with db_session_factory.begin() as session:
        row = await update_routine(
            session,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            destination_kind="dm",
        )
        assert row is not None
        await set_access_policy(
            session,
            tenant_id=row.tenant_id,
            policy=TenantAccessPolicy(agent_rules={"pinned name": AgentRule(runs_in=("456",))}),
        )
    dms = _Dms()

    async def member(*args):
        return Membership(is_member=True)

    async def names(_):
        if agent_state == "missing":
            return ()
        return ("daimon", "pinned name") if agent_state == "pinned_alias" else ("daimon",)

    poster = make_discord_routine_poster(
        db_session_factory,
        fetch_channel=AsyncMock(side_effect=AssertionError("not a channel")),
        open_dm=dms.open,
        dm_policy=lambda _: DirectMessagePolicy(),
        membership_resolver=CachedMembershipResolver(member),
        resolve_agent_names=names,
    )
    result = await poster(row.model_copy(update={"delivery_payload": "private"}))
    assert result.status == ("delivered" if agent_state == "unrelated_rule" else "skipped")
    assert bool(dms.sent) == (agent_state == "unrelated_rule")


@pytest.mark.parametrize("destination", ["channel", "dm"])
@pytest.mark.parametrize("scenario", ["success", "failure", "oversize"])
async def test_generated_files_share_the_at_most_once_outbox(
    db_session, db_session_factory, destination, scenario
):
    from datetime import UTC, datetime

    import httpx
    from daimon.core.config import DirectMessagePolicy
    from daimon.core.routine_delivery import poll_deliveries_once
    from daimon.core.routine_files import PREFIX, RoutineFile, RoutinePayload
    from daimon.core.stores.routines import get_routine, update_routine
    from daimon.testing.ma import MARouter
    from mux.testing.drivers.anthropic.ma import build_fake_anthropic

    row = await _routine(db_session)
    payload = (
        PREFIX
        + RoutinePayload(
            text="Report",
            files=[
                RoutineFile(
                    file_id="file_report",
                    filename="report.csv",
                    mime_type="text/csv",
                    size_bytes=11 * 1024 * 1024 if scenario == "oversize" else 4,
                )
            ],
        ).model_dump_json()
    )
    async with db_session_factory.begin() as session:
        if destination == "dm":
            await update_routine(
                session,
                row.id,
                edited_by=row.created_by_user_id,
                tenant_id=row.tenant_id,
                destination_kind="dm",
            )
        await record_result(
            session, row.id, tail="Report", error=None, delivery="pending", delivery_payload=payload
        )
    router = MARouter()
    router.add(
        "GET", r"/v1/files/([^/]+)/content", lambda r, m: httpx.Response(200, content=b"data")
    )
    channel = _text_channel()
    channel.guild.filesize_limit = 10 * 1024 * 1024
    dm = MagicMock(spec=discord.DMChannel)
    dm.guild = None
    dm.send = AsyncMock()
    destination_transport = dm if destination == "dm" else channel
    uploaded = []

    async def send(**kwargs):
        if "file" in kwargs:
            uploaded.append(kwargs)
            if scenario == "failure":
                raise RuntimeError("uncertain upload")
        return SimpleNamespace(id=123)

    destination_transport.send = AsyncMock(side_effect=send)

    async def membership(*args):
        return Membership(is_member=True)

    poster = make_discord_routine_poster(
        db_session_factory,
        fetch_channel=AsyncMock(return_value=channel),
        open_dm=AsyncMock(return_value=dm),
        dm_policy=lambda _: DirectMessagePolicy(mode="members"),
        membership_resolver=CachedMembershipResolver(membership),
        output_client=build_fake_anthropic(router.dispatch),
    )
    for _ in range(2):
        await poll_deliveries_once(
            db_session_factory, platform="discord", post=poster, now=datetime.now(UTC)
        )
    if scenario == "oversize":
        assert not uploaded
        notices = [
            call.kwargs.get("content", "") for call in destination_transport.send.await_args_list
        ]
        assert any("upload limit" in text for text in notices)
    else:
        assert len(uploaded) == 1
        assert uploaded[0]["file"].fp.read() == b"data"
        assert uploaded[0]["allowed_mentions"].everyone is False
    if destination == "dm":
        channel.send.assert_not_awaited()
    else:
        dm.send.assert_not_awaited()
    async with db_session_factory() as session:
        after = await get_routine(session, row.id, tenant_id=row.tenant_id)
    assert after.delivery_status == ("skipped" if scenario == "failure" else "delivered")


async def test_destination_change_between_text_and_file_cancels_payload(
    db_session, db_session_factory
):
    from datetime import UTC, datetime

    from daimon.core.config import DirectMessagePolicy
    from daimon.core.routine_delivery import poll_deliveries_once
    from daimon.core.routine_files import PREFIX, RoutineFile, RoutinePayload
    from daimon.core.stores.routines import get_routine, update_routine
    from mux.testing.drivers.anthropic.ma import build_fake_anthropic

    row = await _routine(db_session)
    payload = (
        PREFIX
        + RoutinePayload(
            text="Report",
            files=[
                RoutineFile(
                    file_id="file_report", filename="report.csv", mime_type="text/csv", size_bytes=4
                )
            ],
        ).model_dump_json()
    )
    async with db_session_factory.begin() as session:
        await record_result(
            session, row.id, tail="Report", error=None, delivery="pending", delivery_payload=payload
        )
    channel = _text_channel()
    channel.guild.filesize_limit = 10 * 1024 * 1024

    async def send(**kwargs):
        assert "file" not in kwargs
        async with db_session_factory.begin() as session:
            await update_routine(
                session,
                row.id,
                edited_by=row.created_by_user_id,
                tenant_id=row.tenant_id,
                destination_kind="channel",
                destination_id="777",
            )
        return SimpleNamespace(id=123)

    channel.send = AsyncMock(side_effect=send)
    reads = []

    def no_reads(request):
        reads.append(request)
        raise AssertionError("a cancelled payload must not download files")

    async def membership(*args):
        return Membership(is_member=True)

    poster = make_discord_routine_poster(
        db_session_factory,
        fetch_channel=AsyncMock(return_value=channel),
        open_dm=AsyncMock(side_effect=AssertionError("no fallback after changing destination")),
        dm_policy=lambda _: DirectMessagePolicy(mode="members"),
        membership_resolver=CachedMembershipResolver(membership),
        output_client=build_fake_anthropic(no_reads),
    )
    await poll_deliveries_once(
        db_session_factory, platform="discord", post=poster, now=datetime.now(UTC)
    )
    assert not reads
    channel.send.assert_awaited_once()
    async with db_session_factory() as session:
        after = await get_routine(session, row.id, tenant_id=row.tenant_id)
    assert after.destination_id == "777"
    assert after.delivery_payload is None


@pytest.mark.parametrize(
    "revocation", ["dm_disabled", "isolation", "dm_policy", "recipient", "signed_error"]
)
async def test_fallback_files_recheck_privacy_after_download(
    db_session, db_session_factory, monkeypatch, revocation
):
    from datetime import UTC, datetime

    from anthropic import AsyncAnthropic
    from daimon.core.access_policy import ChannelRule
    from daimon.core.config import DirectMessagePolicy
    from daimon.core.routine_delivery import poll_deliveries_once
    from daimon.core.routine_files import PREFIX, RoutineFile, RoutinePayload
    from daimon.core.stores.direct_messages import set_dm_enabled
    from daimon.core.stores.routines import get_routine
    from structlog.testing import capture_logs

    row = await _routine(db_session, destination_kind="thread")
    payload = (
        PREFIX
        + RoutinePayload(
            text="Report",
            files=[
                RoutineFile(
                    file_id="file_report", filename="report.csv", mime_type="text/csv", size_bytes=4
                )
            ],
        ).model_dump_json()
    )
    async with db_session_factory.begin() as session:
        await record_result(
            session, row.id, tail="Report", error=None, delivery="pending", delivery_payload=payload
        )
    thread = _uncached_thread(parent_id=444)
    parent = _text_channel(perms=_perms(send=False))
    dm = MagicMock(spec=discord.DMChannel)
    dm.guild = None
    dm.send = AsyncMock()
    eligible = True
    policy = DirectMessagePolicy(mode="members")
    downloads = []
    completed = []
    sentinel = "https://upload.invalid/signed?token=secret-sentinel"

    async def download(*args, **kwargs):
        nonlocal eligible, policy
        downloads.append(True)
        async with db_session_factory.begin() as session:
            if revocation == "dm_disabled":
                await set_dm_enabled(session, tenant_id=row.tenant_id, enabled=False)
            elif revocation == "isolation":
                # The outbox does not store this resolved thread parent.
                await set_access_policy(
                    session,
                    tenant_id=row.tenant_id,
                    policy=TenantAccessPolicy(
                        channel_rules={"444": ChannelRule(readers="own", writers="own")}
                    ),
                )
        if revocation == "dm_policy":
            policy = DirectMessagePolicy(mode="disabled")
        if revocation == "recipient":
            eligible = False
        if revocation == "signed_error":
            raise discord.HTTPException(MagicMock(status=500, reason="failed"), sentinel)
        completed.append(True)
        return b"data"

    async def membership(*args):
        return Membership(is_member=eligible)

    async def fetch(channel_id):
        return thread if channel_id == 555 else parent

    monkeypatch.setattr("daimon.core.routine_files.download_output_file", download)
    poster = make_discord_routine_poster(
        db_session_factory,
        fetch_channel=fetch,
        open_dm=AsyncMock(return_value=dm),
        dm_policy=lambda _: policy,
        membership_resolver=CachedMembershipResolver(membership),
        output_client=AsyncAnthropic(api_key="sk-test"),
    )
    with capture_logs() as logs:
        await poll_deliveries_once(
            db_session_factory, platform="discord", post=poster, now=datetime.now(UTC)
        )
    assert downloads == [True], "the first guard must pass before revocation during download"
    assert all("file" not in call.kwargs for call in dm.send.await_args_list)
    assert completed == ([] if revocation == "signed_error" else [True])
    assert sentinel not in str(logs)
    async with db_session_factory() as session:
        after = await get_routine(session, row.id, tenant_id=row.tenant_id)
    assert after.delivery_status == "skipped"


@pytest.mark.parametrize("marked", ["destination", "parent-found-at-delivery"])
async def test_a_shared_channels_routine_never_leaves_it_by_dm(
    marked: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The fire ran held to the channel. A thread saved without its parent is
    placed at delivery, and the parent's recorded sharing holds it too."""
    from daimon.core.stores.shared_channels import mark_channel_shared

    row = await _routine(db_session)
    await mark_channel_shared(
        db_session,
        tenant_id=row.tenant_id,
        platform="discord",
        channel_id="555" if marked == "destination" else "444",
        source="platform",
    )
    await db_session.commit()
    dms = _Dms()
    if marked == "destination":
        channels: object = discord.NotFound(MagicMock(status=404, reason="Not Found"), "gone")
        expected = "destination_unavailable"
    else:
        channels = {555: _uncached_thread(private=True), 444: _text_channel()}
        expected = "creator_cannot_post"

    outcome = await _poster(db_session_factory, channels, dms=dms)(row)

    assert (outcome.status, outcome.note) == ("skipped", expected)
    assert dms.sent == [], "the result never goes by DM"
