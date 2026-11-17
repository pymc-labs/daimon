"""Can Daimon post in the channel requests go to, and what fixes it when not. Pure.

Shells fetch the platform's facts (Discord permission bits, Slack's channel record, a
Teams pick) and pass them in. The rules mirror what each chat adapter needs at post time:
Discord needs View Channel, Send Messages and Embed Links; Slack needs the channel to
exist and not be archived, and Daimon in a private one (a public channel is joined at post
time); Teams needs the pick's team id and service URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from daimon.core.channel_visibility import (
    DISCORD_ADMINISTRATOR,
    DISCORD_EMBED_LINKS,
    DISCORD_SEND_MESSAGES,
    DISCORD_VIEW_CHANNEL,
)

UNVERIFIED_DETAIL: Final = "Couldn't check. Try again."

_DISCORD_NEEDS: Final = (
    (DISCORD_VIEW_CHANNEL, "View Channel"),
    (DISCORD_SEND_MESSAGES, "Send Messages"),
    (DISCORD_EMBED_LINKS, "Embed Links"),
)


@dataclass(frozen=True)
class ChannelCheck:
    """`ready`; `blocked` with the fix in `detail`; `unverified` when the platform could not say."""

    state: Literal["ready", "blocked", "unverified"]
    detail: str | None = None


READY: Final = ChannelCheck("ready")
UNVERIFIED: Final = ChannelCheck("unverified", UNVERIFIED_DETAIL)


def _blocked(detail: str) -> ChannelCheck:
    return ChannelCheck("blocked", detail)


def _joined(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def discord_check(*, bits: int, label: str) -> ChannelCheck:
    """Daimon's permission bits in the channel against what posting needs."""
    if bits & DISCORD_ADMINISTRATOR:
        return READY
    missing = [name for bit, name in _DISCORD_NEEDS if not bits & bit]
    if not missing:
        return READY
    return _blocked(f"Give Daimon {_joined(missing)} in {label}.")


def discord_not_text(label: str) -> ChannelCheck:
    return _blocked(f"{label} isn't a text channel.")


def discord_missing(label: str) -> ChannelCheck:
    """The channel is gone, or Daimon is not in its server."""
    return _blocked(f"Daimon can't find {label}. Pick another channel.")


def slack_check(
    *,
    label: str,
    found: bool,
    is_archived: bool = False,
    is_private: bool = False,
    bot_is_member: bool = False,
) -> ChannelCheck:
    """Slack's record of the channel: a private one (or one Daimon can't see) needs an invite."""
    if not found:
        return _blocked(f"Invite Daimon to {label}.")
    if is_archived:
        return _blocked(f"{label} is archived.")
    if is_private and not bot_is_member:
        return _blocked(f"Invite Daimon to {label}.")
    return READY


def teams_check(*, label: str, team_id: str | None, service_url: str | None) -> ChannelCheck:
    """A Teams pick must carry the team and the service URL chat posts through."""
    missing = [
        name for name, value in (("team id", team_id), ("service URL", service_url)) if not value
    ]
    if not missing:
        return READY
    return _blocked(f"Pick {label} again to record its {_joined(missing)}.")
