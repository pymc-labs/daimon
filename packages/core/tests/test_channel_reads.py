"""A channel page's extra reads: spend three ways, its routines, its link, its subscription."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core.channel_reads import (
    channel_routines,
    channel_spend,
    platform_link,
    teams_subscription,
)
from daimon.core.stores import routines
from daimon.testing.factories import make_tenant, make_usage_event
from sqlalchemy.ext.asyncio import AsyncSession


def test_platform_links_open_the_channel_in_its_app() -> None:
    assert platform_link("discord", workspace_id="1", channel_id="2") == (
        "https://discord.com/channels/1/2"
    )
    assert platform_link("slack", workspace_id="T1", channel_id="C1:171.1") == (
        "https://slack.com/app_redirect?channel=C1&team=T1"
    )
    teams = platform_link("teams", workspace_id="t", channel_id="19:a@thread", name="Ops room")
    assert teams == "https://teams.microsoft.com/l/channel/19%3Aa%40thread/Ops%20room?tenantId=t"
    assert platform_link("discord", workspace_id="1", channel_id="web") is None


async def test_spend_splits_by_day_agent_and_person(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    for agent, user in (("a1", "u1"), ("a1", "u2"), ("a2", "u1")):
        await make_usage_event(
            db_session, tenant=tenant, channel_id="c1", agent_id=agent, platform_user_id=user
        )
    await make_usage_event(db_session, tenant=tenant, channel_id="c2", agent_id="a1")
    spend = await channel_spend(
        db_session,
        tenant_id=tenant.id,
        channel_id="c1",
        since=datetime.now(UTC) - timedelta(days=1),
    )
    total = sum(spend.by_day.values())
    assert total > 0 and len(spend.by_day) == 1
    assert sum(spend.by_agent.values()) == total == sum(spend.by_person.values())
    assert spend.by_agent["a1"] == 2 * spend.by_agent["a2"], "c2's spend is not counted"


async def test_routines_here_say_whether_they_post_or_were_made_here(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)

    async def make(name: str, *, posts: str | None, made: str | None) -> None:
        await routines.create_routine(
            db_session,
            tenant_id=tenant.id,
            created_by_user_id="u1",
            agent_id="agent-1",
            agent_name=name,
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="hello",
            enabled=True,
            next_fire_at=datetime.now(UTC) + timedelta(hours=1),
            destination_kind="channel" if posts else None,
            destination_id=posts,
            channel_id=posts,
            made_in_channel_id=made,
        )

    await make("posts", posts="c1", made="c9")
    await make("made", posts=None, made="c1")
    await make("both", posts="c1", made="c1")
    await make("elsewhere", posts="c2", made="c2")
    found = await channel_routines(db_session, tenant_id=tenant.id, channel_id="c1")
    assert sorted((r.agent_name, rel) for r, rel in found) == [
        ("both", "both"),
        ("made", "made_here"),
        ("posts", "posts_here"),
    ]


async def test_only_private_and_shared_teams_channels_have_a_subscription(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    now = datetime.now(UTC)

    async def read(kind: str):
        return await teams_subscription(
            db_session, tenant_id=tenant.id, channel_id="19:a@thread", kind=kind, now=now
        )

    assert await read("standard") is None
    assert await read("private") == ("missing", None)
