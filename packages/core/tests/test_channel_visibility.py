"""Discord's view rule: base role permissions, then each channel's overwrites."""

from __future__ import annotations

from daimon.core.channel_visibility import (
    DISCORD_ADMINISTRATOR,
    DISCORD_VIEW_CHANNEL,
    DiscordMember,
    DiscordOverwrite,
    discord_visible_channels,
)

GUILD = "g1"
VIEW = DISCORD_VIEW_CHANNEL


def _visible(member: DiscordMember, roles: dict[str, int]) -> frozenset[str]:
    overwrites = {
        "open": (),
        "hidden": (DiscordOverwrite(GUILD, 0, allow=0, deny=VIEW),),
        "team": (
            DiscordOverwrite(GUILD, 0, allow=0, deny=VIEW),
            DiscordOverwrite("team", 0, allow=VIEW, deny=0),
        ),
        "not-you": (DiscordOverwrite(member.user_id, 1, allow=0, deny=VIEW),),
    }
    return discord_visible_channels(
        member,
        guild_id=GUILD,
        role_permissions=roles,
        channel_overwrites=overwrites,
        channel_ids=(*overwrites, "gone"),
    )


def test_overwrites_hide_and_reveal_channels() -> None:
    roles = {GUILD: VIEW, "team": 0, "boss": DISCORD_ADMINISTRATOR}
    plain = DiscordMember("u1", frozenset())

    assert _visible(plain, roles) == {"open"}, "@everyone denied, own overwrite denied"
    assert _visible(DiscordMember("u1", frozenset({"team"})), roles) == {"open", "team"}
    assert _visible(DiscordMember("u1", frozenset({"boss"})), roles) == {
        "open",
        "hidden",
        "team",
        "not-you",
    }, "an administrator sees every channel the guild has, never one it lacks"
    assert _visible(DiscordMember("u1", frozenset(), is_owner=True), roles) >= {"hidden"}
