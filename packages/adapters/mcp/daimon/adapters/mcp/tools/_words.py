"""The words a tool result uses for the caller's platform.

Tool results are built per call, so they name who an admin is and what the
tenant is called in the caller's own platform (`daimon.core.platform_words`).
A caller with no chat platform (a CLI or MCP client) gets neutral words.
"""

from __future__ import annotations

from daimon.core.platform_words import (
    NEUTRAL_ADMIN,
    NEUTRAL_SPACE,
    Place,
    PlatformWords,
    words_for,
)
from daimon.core.stores.domain import CHAT_PLATFORMS

__all__ = ["admin_noun", "setup_instruction", "space_noun", "top_up_noun"]

_NEUTRAL_ADMIN = NEUTRAL_ADMIN
_NEUTRAL_SPACE = NEUTRAL_SPACE
_NEUTRAL_SETUP = "open the setup panel"
_NEUTRAL_TOP_UP = "the billing command"


def _words(platform: str | None) -> PlatformWords | None:
    return words_for(platform) if platform in CHAT_PLATFORMS else None


def admin_noun(platform: str | None) -> str:
    """Who can change settings, e.g. "a server admin"; "an admin" for an unknown platform."""
    words = _words(platform)
    return words.admin if words is not None else _NEUTRAL_ADMIN


def space_noun(platform: str | None) -> str:
    """What Daimon is installed in, e.g. "server"; "workspace" for an unknown platform."""
    words = _words(platform)
    return words.space if words is not None else _NEUTRAL_SPACE


def top_up_noun(platform: str | None) -> str:
    """Where an admin adds credit, as a noun."""
    words = _words(platform)
    return words.top_up() if words is not None else _NEUTRAL_TOP_UP


def setup_instruction(
    platform: str | None, *, place: Place = "channel", foreign: bool | None = False
) -> str:
    """How to reach the setup panel, e.g. "run `/agent-setup`" or
    "send `setup` in a 1:1 chat with Daimon". For a `foreign` sender (from another
    organisation, with no 1:1 chat) it says who to ask instead, and when that is not
    known (None) on Teams it names no chat at all."""
    if foreign is None and platform == "teams":
        return _NEUTRAL_SETUP
    words = _words(platform)
    said = words.say("setup", place=place, me="Daimon", foreign=foreign is True) if words else None
    return said or _NEUTRAL_SETUP
