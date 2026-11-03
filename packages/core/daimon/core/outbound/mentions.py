"""The one mention-rendering step every outgoing post goes through.

It turns broadcasts (everyone, here, roles, groups) into plain text, turns
mentions of people who opted out of tags into plain ``@Name``, names who
asked for a tool post, and returns the people the post may ping, so the
platform send lists them explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Literal

from pydantic import BaseModel, ConfigDict

Platform = Literal["discord", "slack", "teams"]

_DISCORD_USER = re.compile(r"<@!?(\d+)>")
_SLACK_USER = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]*))?>")
_SLACK_BROADCAST = re.compile(r"<!(channel|here|everyone)(?:\|[^>]*)?>")
_SLACK_GROUP = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|@?([^>]*))?>")
_TEAMS_AT = re.compile(r"<at[^>]*>(.*?)</at>", re.IGNORECASE | re.DOTALL)
# Discord pings these from plain text; the zero-width space keeps the words readable.
_DISCORD_BROADCAST = re.compile(r"@(everyone|here)\b")
_LINK = re.compile(r"https?://\S+|<[^>\s]*>")


# The people a Discord send may ping, set once a post is rendered: the
# attribution names the requester without pinging them.
ping_users: ContextVar[frozenset[str] | None] = ContextVar("ping_users", default=None)


class RenderedPost(BaseModel):
    """A post ready to send, and the people it may ping."""

    model_config = ConfigDict(frozen=True)

    text: str
    user_ids: tuple[str, ...] = ()


def neutralise_broadcasts(text: str, platform: Platform) -> str:
    """Broadcast and group mentions as plain words that ping nobody."""
    if platform == "slack":
        text = _SLACK_BROADCAST.sub(lambda m: f"@{m.group(1)}", text)
        return _SLACK_GROUP.sub(lambda m: f"@{m.group(1) or 'group'}", text)
    if platform == "teams":
        # Tool posts carry no mention entities, so no tag pings; keep the name.
        return _TEAMS_AT.sub(lambda m: f"@{m.group(1)}", text)
    # Role tags stay readable: every Discord send turns role pings off.
    return _DISCORD_BROADCAST.sub(lambda m: f"@\u200b{m.group(1)}", text)


def mentioned_user_ids(text: str, platform: Platform) -> tuple[str, ...]:
    """Distinct people a post's text mentions, in order."""
    if platform == "teams":
        return ()
    pattern = _SLACK_USER if platform == "slack" else _DISCORD_USER
    return tuple(dict.fromkeys(m.group(1) for m in pattern.finditer(text)))


def _untag(text: str, platform: Platform, skip: frozenset[str], names: Mapping[str, str]) -> str:
    if not skip or platform == "teams":
        return text
    pattern = _SLACK_USER if platform == "slack" else _DISCORD_USER

    def plain(match: re.Match[str]) -> str:
        user_id = match.group(1)
        if user_id not in skip:
            return match.group(0)
        label = names.get(user_id) or (match.group(2) if platform == "slack" else None)
        return f"@{label or 'someone'}"

    return pattern.sub(plain, text)


def attribution_line(
    platform: Platform,
    *,
    requester_id: str | None = None,
    requester_name: str | None = None,
    routine_name: str | None = None,
) -> str | None:
    """The last line of a tool or routine post: who asked for it, without a ping."""
    if platform == "discord" and requester_id:
        return f"-# requested by <@{requester_id}>"
    if requester_name:
        prefix = "-# " if platform == "discord" else ""
        return f"{prefix}requested by {requester_name}"
    if routine_name:
        prefix = "-# " if platform == "discord" else ""
        return f'{prefix}from the routine "{routine_name}"'
    return None


def render_post(
    text: str,
    platform: Platform,
    *,
    do_not_tag: frozenset[str] = frozenset(),
    names: Mapping[str, str] | None = None,
    attribution: str | None = None,
) -> RenderedPost:
    """Apply every mention rule; ``attribution`` is appended once, never pinging."""
    body = _untag(neutralise_broadcasts(text, platform), platform, do_not_tag, names or {})
    if attribution and body.rstrip().endswith(attribution):
        body = body.rstrip().removesuffix(attribution).rstrip()
    user_ids = mentioned_user_ids(body, platform)
    if attribution:
        body = f"{body}\n{attribution}" if body.strip() else attribution
    return RenderedPost(text=body, user_ids=user_ids)


def caption_length(text: str, platform: Platform) -> int:
    """Characters of a caption that say something: mentions and links left out."""
    pattern = _SLACK_USER if platform == "slack" else _DISCORD_USER
    stripped = _LINK.sub("", pattern.sub("", text))
    return len(" ".join(stripped.split()))
