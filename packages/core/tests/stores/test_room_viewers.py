"""Who can view each Discord room: computed from roles and overwrites, stored per generation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.core.audience_facts import AudienceChannel, AudienceMember, compute_room_viewers
from daimon.core.channel_visibility import DISCORD_VIEW_CHANNEL, DiscordOverwrite
from daimon.core.stores import audience_facts as facts_store
from daimon.core.stores import room_viewers as store
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

GUILD, TEAM = "100", "200"
NOW = datetime(2026, 5, 1, tzinfo=UTC)
VIEW = DISCORD_VIEW_CHANNEL


def test_viewers_follow_roles_and_overwrites_and_leave_out_bots() -> None:
    """@everyone views `open`; `team` denies @everyone and allows the team role, and names
    one person allowed and one team member denied. Bots never count; threads follow."""
    members = [
        AudienceMember("1", frozenset({TEAM})),
        AudienceMember("2", frozenset({TEAM})),
        AudienceMember("3", frozenset()),
        AudienceMember("4", frozenset(), is_bot=True),
    ]
    team = (
        DiscordOverwrite(GUILD, 0, 0, VIEW),
        DiscordOverwrite(TEAM, 0, VIEW, 0),
        DiscordOverwrite("3", 1, VIEW, 0),
        DiscordOverwrite("2", 1, 0, VIEW),
    )
    channels = [
        AudienceChannel("10", 0, "open"),
        AudienceChannel("11", 0, "team", overwrites=team),
        AudienceChannel("12", 11, "public thread", parent_id="11"),
        AudienceChannel("13", 12, "private thread", parent_id="11"),
    ]
    viewers = compute_room_viewers(
        guild_id=GUILD,
        members=members,
        role_permissions={GUILD: VIEW, TEAM: 0},
        channels=channels,
        cap=200,
    )
    assert viewers["10"] == {"1", "2", "3"}, "everyone but the bot"
    assert viewers["11"] == {"1", "3"}, "the role, minus the denied member, plus the named one"
    assert viewers["12"] == viewers["11"], "a public thread has its parent's viewers"
    assert "13" not in viewers, "a private thread is unknown"
    capped = compute_room_viewers(
        guild_id=GUILD, members=members, role_permissions={GUILD: VIEW}, channels=channels, cap=2
    )
    assert "10" not in capped, "a room over the cap is unknown"


async def test_viewers_are_read_only_under_the_current_complete_generation(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="3101")
    generation = await facts_store.publish_facts(
        db_session, tenant_id=tenant.id, rooms={}, now=NOW, fresh_until=NOW + timedelta(minutes=3)
    )
    await store.publish_viewers(
        db_session,
        tenant_id=tenant.id,
        generation=generation,
        viewers={"10": frozenset({"1", "2"})},
        now=NOW,
    )
    read = store.read_viewers
    assert await read(db_session, tenant_id=tenant.id, channel_id="10", now=NOW) == {"1", "2"}
    assert await read(db_session, tenant_id=tenant.id, channel_id="11", now=NOW) is None
    late = NOW + timedelta(minutes=5)
    assert await read(db_session, tenant_id=tenant.id, channel_id="10", now=late) is None, "lapsed"
    await facts_store.mark_unknown(db_session, tenant_id=tenant.id, now=NOW)
    assert await read(db_session, tenant_id=tenant.id, channel_id="10", now=NOW) is None, (
        "a change that makes the facts unknown makes the viewers unknown"
    )
