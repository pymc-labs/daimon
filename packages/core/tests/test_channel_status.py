"""Deleted and direct channels: the status round, the directory's status, kind and path."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core.channel_directory import (
    PathNode,
    channel_kind,
    channel_path,
    load_channel_directory,
    load_channel_row,
)
from daimon.core.channel_listing import check_unlisted_channels, discord_channel_answer
from daimon.core.platform_names import ChannelStatus
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.domain import ChannelNameRow
from daimon.core.stores.platform_names import (
    ChannelSighting,
    get_channel_names,
    list_channel_names,
    upsert_channel_names,
)
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DEFAULT = DeploymentDefault(agent_name="daimon")
NOW = datetime(2026, 5, 20, 12, tzinfo=UTC)


def test_discord_answers_read_as_a_name_gone_direct_or_nothing() -> None:
    assert discord_channel_answer(404, {"code": 10003}, "g1") is ChannelStatus.GONE
    assert discord_channel_answer(404, {"code": 10004}, "g1") is None, "another 404 is no answer"
    assert discord_channel_answer(403, {}, "g1") is None, "a refusal is no answer"
    assert discord_channel_answer(200, {"type": 1}, "g1") is ChannelStatus.DIRECT
    assert discord_channel_answer(200, {"type": 3}, "g1") is ChannelStatus.DIRECT
    assert discord_channel_answer(200, {"guild_id": "g2", "name": "x"}, "g1") is None
    assert discord_channel_answer(200, {"guild_id": "g1", "name": "ops"}, "g1") == "ops"


async def _configure(session: AsyncSession, tenant_id: object, *channel_ids: str) -> None:
    for channel_id in channel_ids:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),  # pyright: ignore[reportArgumentType]
            tenant_id=tenant_id,  # pyright: ignore[reportArgumentType]
            agent_name="local",
            mode="agent",
        )


async def test_the_round_marks_gone_and_direct_and_skips_what_the_listing_showed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await _configure(db_session, tenant.id, "live", "gone", "dm", "kept", "again")
    await db_session.commit()
    asked: list[str] = []

    async def lookup(_workspace: str, channel_id: str) -> str | ChannelStatus | None:
        asked.append(channel_id)
        return {
            "gone": ChannelStatus.GONE,
            "dm": ChannelStatus.DIRECT,
            "kept": "kept-channel",
            "again": ChannelStatus.GONE,
        }[channel_id]

    marked = await check_unlisted_channels(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        external_id=tenant.external_id,
        listed={"live"},
        lookup=lookup,
    )
    assert marked == 3 and sorted(asked) == ["again", "dm", "gone", "kept"], (
        "a channel the listing showed (a live thread too) is not asked about"
    )
    rows = {
        r.channel_id: r
        for r in await list_channel_names(
            db_session, tenant_id=tenant.id, platform="discord", unnamed=True
        )
    }
    assert {k: v.status for k, v in rows.items()} == {
        "gone": "gone",
        "dm": "direct",
        "kept": "named",
        "again": "gone",
    }
    assert rows["kept"].name == "kept-channel"
    assert "gone" not in await get_channel_names(
        db_session, tenant_id=tenant.id, platform="discord", channel_ids=["gone"]
    ), "an unnamed row has no name"

    asked.clear()
    await check_unlisted_channels(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        external_id=tenant.external_id,
        listed=set(),
        lookup=lookup,
    )
    assert sorted(asked) == ["kept", "live"], "gone and direct channels are not asked again"


async def test_a_round_asks_at_most_its_limit_and_survives_a_failed_ask(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await _configure(db_session, tenant.id, *(f"c{i}" for i in range(6)))
    await db_session.commit()
    asked: list[str] = []

    async def lookup(_workspace: str, channel_id: str) -> str | ChannelStatus | None:
        asked.append(channel_id)
        if len(asked) == 1:
            raise RuntimeError("platform down")
        return ChannelStatus.GONE

    marked = await check_unlisted_channels(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        external_id=tenant.external_id,
        listed=set(),
        lookup=lookup,
        limit=4,
    )
    assert len(asked) == 4 and marked == 3, "four asked, the failed one stores nothing"


async def test_the_directory_leaves_deleted_and_direct_channels_out(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await _configure(db_session, tenant.id, "gone", "dm", "live")
    await upsert_channel_names(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        names={"live": "ops", "history": ChannelSighting("never-configured")},
    )
    await db_session.commit()

    async def lookup(_workspace: str, channel_id: str) -> str | ChannelStatus | None:
        if channel_id == "history":
            return "never-configured"
        return ChannelStatus.GONE if channel_id == "gone" else ChannelStatus.DIRECT

    await check_unlisted_channels(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        external_id=tenant.external_id,
        listed={"live"},
        lookup=lookup,
    )
    directory = await load_channel_directory(
        db_session, tenant_id=tenant.id, default=DEFAULT, now=NOW, markup=Decimal(2)
    )
    assert {r.channel_id for r in directory.rows} == {"web", "live", "history"}, (
        "a channel the platform deleted, and a DM, are not listed"
    )
    assert all(r.status == "active" for r in directory.rows)
    for channel_id, status in (("gone", "gone"), ("dm", "direct")):
        row = await load_channel_row(
            db_session,
            tenant_id=tenant.id,
            channel_id=channel_id,
            default=DEFAULT,
            now=NOW,
            markup=Decimal(2),
        )
        assert row is not None and row.status == status, "a direct link still reads it"


def _row(
    channel_id: str, name: str, kind: str, parent: tuple[str, str] | None = None
) -> ChannelNameRow:
    return ChannelNameRow(
        platform="discord",
        channel_id=channel_id,
        name=name,
        kind=kind,
        parent_id=parent[0] if parent else None,
        parent_name=parent[1] if parent else None,
    )


@pytest.mark.parametrize(
    ("thread_parent_kind", "kind"), [("text", "thread"), ("forum", "forum_post")]
)
def test_a_discord_thread_sits_under_its_channel_and_category(
    thread_parent_kind: str, kind: str
) -> None:
    names = {
        "ch": _row("ch", "clv-models", thread_parent_kind, ("cat", "Projects")),
        "th": _row("th", "Q3 holdout", "thread", ("ch", "clv-models")),
    }
    assert channel_kind("discord", "th", names["th"], names) == kind
    assert channel_path("discord", "th", names["th"], names) == (
        PathNode("cat", "Projects", "category"),
        PathNode("ch", "clv-models", "channel"),
    )
    assert channel_kind("discord", "ch", names["ch"], names) == "channel"
    assert channel_path("discord", "ch", names["ch"], names) == (
        PathNode("cat", "Projects", "category"),
    )


def test_a_thread_whose_channel_has_no_row_is_named_by_what_it_recorded() -> None:
    names = {"th": _row("th", "Weekly sync", "thread", ("ops", "ops-notes"))}
    assert channel_path("discord", "th", names["th"], names) == (
        PathNode("ops", "ops-notes", "channel"),
    )
    assert channel_path("discord", "x", None, names) == (), "an unnamed channel has no path"


def test_a_teams_channel_sits_under_its_team_and_a_reply_under_its_channel() -> None:
    chan = ChannelNameRow(
        platform="teams",
        channel_id="19:ops",
        name="ops-room",
        kind="standard",
        parent_id="19:team",
        parent_name="Ops team",
    )
    reply = chan.model_copy(update={"channel_id": "19:ops;messageid=1", "name": "Incident 14"})
    names = {chan.channel_id: chan, reply.channel_id: reply}
    team = PathNode("19:team", "Ops team", "team")
    assert channel_path("teams", "19:ops", chan, names) == (team,)
    assert channel_path("teams", reply.channel_id, reply, names) == (
        team,
        PathNode("19:ops", "ops-room", "channel"),
    )
    assert channel_kind("teams", reply.channel_id, reply, names) == "thread"
