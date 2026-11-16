"""What each chat platform calls things, and how a person reaches each action there.

Help cards, refusals, tool results and the per-turn note to the model read this
one table, so a Teams person never sees a Discord command and an action has the
same description everywhere. Static text the model reads (the system prompt,
skills, tool descriptions) stays platform-neutral and points at the per-turn note.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from daimon.core.stores.domain import CHAT_PLATFORMS, ChatPlatform

#: Where a message was sent: a private chat with Daimon, a channel, or a group chat.
Place = Literal["dm", "channel", "group"]

Action = Literal[
    "here",
    "setup",
    "github",
    "memory",
    "model",
    "routines",
    "dm_me",
    "billing",
    "settings",
    "privacy",
    "new",
    "dm_settings",
    "support",
    "help",
]

#: What a caller with no chat platform (the CLI, an MCP client) is told: no platform's
#: own word, and no command.
NEUTRAL_SPACE = "workspace"
NEUTRAL_ADMIN = "an admin"

#: Where a command works: anywhere, only in a channel of the space, or only in a private chat.
Reach = Literal["anywhere", "space", "chat"]

#: One description per action, shown the same on every platform.
DESCRIPTIONS: Mapping[Action, str] = MappingProxyType(
    {
        "here": "Who answers here and what it can use",
        "setup": "Your agents and where they answer",
        "github": "Connect GitHub repos",
        "memory": "What the agent remembers",
        "model": "This conversation's model",
        "routines": "Scheduled jobs",
        "dm_me": "Continue privately (an admin turns it on)",
        "billing": "Your usage and credit",
        "settings": "Turn features on or off here",
        "privacy": "See, export or delete your data",
        "new": "Start a fresh conversation",
        "dm_settings": "Switch server or start over in our DM",
        "support": "Ask a person",
        "help": "This list",
    }
)


@dataclass(frozen=True, slots=True)
class Command:
    """What a person types for an action, and where it works."""

    word: str
    reach: Reach = "anywhere"


@dataclass(frozen=True, slots=True)
class PlatformWords:
    """One platform's nouns and commands.

    `label` is the platform's name as people write it, and `works_in` says where Daimon
    works on it, so the one-platform rule never denies a place it does work.
    `space` is what Daimon is installed in; `admin` is who can change settings, as
    a person reads it; `admin_rule` says who counts as one, for the model.
    `private_chat` names a private chat with Daimon. `commands` holds only the
    actions this platform has; Teams `support` is registered only when human
    support is configured, so callers that list it check that first.
    """

    platform: ChatPlatform
    label: str
    works_in: str
    space: str
    admin: str
    admin_rule: str
    private_chat: str
    verb: str
    commands: Mapping[Action, Command] = field(default_factory=lambda: MappingProxyType({}))

    def command(self, action: Action) -> Command | None:
        return self.commands.get(action)

    @property
    def host_admin(self) -> str:
        """Who acts for someone from another organisation, who has no private chat with Daimon."""
        return f"someone in the {self.space} that added Daimon"

    def phrase(
        self, action: Action, *, place: Place, me: str = "me", foreign: bool = False
    ) -> str | None:
        """The command as a noun, e.g. "`/agent-setup`" or "`setup` in a 1:1 chat with me".

        Names the place to go when the command doesn't work in `place`; None when
        this platform has no command for `action`. For a `foreign` sender (from another
        organisation, with no private chat to go to) it is the bare command."""
        command = self.commands.get(action)
        if command is None:
            return None
        if command.reach == "chat" and place != "dm":
            if foreign:
                return f"`{command.word}`"
            return f"`{command.word}` in a {self.private_chat} with {me}"
        if command.reach == "space" and place == "dm":
            return f"`{command.word}` in a {self.space} channel"
        return f"`{command.word}`"

    def say(
        self, action: Action, *, place: Place, me: str = "me", foreign: bool = False
    ) -> str | None:
        """How to tell a person to reach `action` from `place`, e.g. "run `/agent-setup`".

        A `foreign` sender cannot reach a command that needs a private chat, so it is
        to be asked of someone in the organisation that added Daimon."""
        phrase = self.phrase(action, place=place, me=me, foreign=foreign)
        if phrase is None:
            return None
        if foreign and place != "dm" and self.commands[action].reach == "chat":
            return f"ask {self.host_admin} to {self.verb} {phrase}"
        return f"{self.verb} {phrase}"

    def fresh_start(self, *, place: Place) -> str:
        """How a person starts over in `place`."""
        if self.platform == "teams":
            return "start a new post" if place == "channel" else "send `new`"
        if place == "channel":
            return "start a new thread"
        if self.platform == "discord" and place == "dm":
            return "run `/new`"
        return "ask me to start fresh"

    def top_up(self, *, place: Place = "channel", me: str = "me", foreign: bool = False) -> str:
        """Where an admin adds credit, as a noun. On Teams it points to the 1:1 chat,
        where the billing card does not expire as a private channel reply does; a
        `foreign` sender has no such chat, so it is the bare command."""
        command = self.commands["billing"]
        if self.platform == "teams" and place != "dm":
            if foreign:
                return f"`{command.word}`"
            return f"`{command.word}` in a {self.private_chat} with {me}"
        return self.phrase("billing", place=place) or f"`{command.word}`"

    def say_top_up(self, *, place: Place = "channel", me: str = "me", foreign: bool = False) -> str:
        """How to tell a person to add credit; for a `foreign` sender, who to ask."""
        said = f"{self.verb} {self.top_up(place=place, me=me, foreign=foreign)}"
        if foreign and self.platform == "teams" and place != "dm":
            return f"ask {self.host_admin} to {said}"
        return said


# Discord registers every command but /privacy, /new and /dm-settings as guild-only;
# /new and /dm-settings are DM-only.
_DISCORD: Mapping[Action, Command] = MappingProxyType(
    {
        "here": Command("/here", "space"),
        "setup": Command("/agent-setup", "space"),
        "github": Command("/github connect", "space"),
        "memory": Command("/memory", "space"),
        "model": Command("/model", "space"),
        "routines": Command("/routines", "space"),
        "dm_me": Command("/dm-me", "space"),
        "billing": Command("/billing", "space"),
        "settings": Command("/settings", "space"),
        "privacy": Command("/privacy"),
        "new": Command("/new", "chat"),
        "dm_settings": Command("/dm-settings", "chat"),
        "help": Command("/help", "space"),
    }
)
# Slack runs its commands in DMs too, except /dm-me, which names the channel to continue from.
# A bare /github opens the setup panel's GitHub page; `/github connect [agent]` also works.
_SLACK: Mapping[Action, Command] = MappingProxyType(
    {
        "here": Command("/here"),
        "setup": Command("/agent-setup"),
        "github": Command("/github"),
        "memory": Command("/memory"),
        "routines": Command("/routines"),
        "dm_me": Command("/dm-me", "space"),
        "billing": Command("/billing"),
        "settings": Command("/settings", "space"),
        "privacy": Command("/privacy"),
        "help": Command("/help"),
    }
)

WORDS: Mapping[ChatPlatform, PlatformWords] = MappingProxyType(
    {
        "discord": PlatformWords(
            platform="discord",
            label="Discord",
            works_in="in other servers, channels, threads and DMs",
            space="server",
            admin="a server admin",
            admin_rule="the server owner, or anyone with Manage Server or Administrator",
            private_chat="DM",
            verb="run",
            commands=_DISCORD,
        ),
        "slack": PlatformWords(
            platform="slack",
            label="Slack",
            works_in="in other channels, DMs and workspaces it's installed in",
            space="workspace",
            admin="a workspace admin",
            admin_rule="a workspace admin or owner",
            private_chat="DM",
            verb="run",
            commands=_SLACK,
        ),
        "teams": PlatformWords(
            platform="teams",
            label="Teams",
            works_in="wherever it has been added: other teams, channels, group chats and 1:1 chats",
            space="organisation",
            admin="an admin",
            admin_rule="an owner of Daimon's admin team",
            private_chat="1:1 chat",
            verb="send",
            commands=MappingProxyType(
                {
                    "here": Command("here"),
                    "setup": Command("setup", "chat"),
                    "memory": Command("memory"),
                    "routines": Command("routines"),
                    "billing": Command("billing"),
                    "settings": Command("settings", "space"),
                    "model": Command("model", "space"),
                    "privacy": Command("privacy"),
                    "new": Command("new"),
                    "support": Command("support", "chat"),
                    "help": Command("help"),
                }
            ),
        ),
    }
)


def words_for(platform: ChatPlatform) -> PlatformWords:
    return WORDS[platform]


def space_for(platform: str | None) -> str:
    """The platform's word for its space; the neutral one for a caller with no chat platform."""
    return words_for(platform).space if platform in CHAT_PLATFORMS else NEUTRAL_SPACE


def works_in_sentence(platform: ChatPlatform) -> str:
    """Where Daimon works on this platform, so the model does not deny a place it was added."""
    words = WORDS[platform]
    return f"In {words.label}, Daimon works {words.works_in}."


def turn_words(platform: ChatPlatform, *, place: Place, foreign: bool = False) -> dict[str, str]:
    """The names the model uses when it tells a person where to go, for one turn.

    Rendered into `<turn_controls>` as `words`; the setup skill and the guidance
    tell the model to use these instead of any command it remembers.

    `foreign` marks a sender from another organisation: Teams opens no private chat
    with them, so setup and billing are for someone in the organisation that added Daimon."""
    words = WORDS[platform]
    out = {
        "space": words.space,
        "admin": f"{words.admin} ({words.admin_rule})",
        "private_chat": words.private_chat,
        "fresh_start": words.fresh_start(place=place),
    }
    if foreign and place != "dm":
        out["admin"] = f"{words.host_admin} ({words.admin_rule})"
        out["private_chat"] = f"none: this person is from another {words.space}"
    for action in ("setup", "here", "help"):
        said = words.say(action, place=place, me="Daimon", foreign=foreign)
        if said is not None:
            out[action] = said
    # Credit is added from the billing card, which on Teams lives in the 1:1 chat.
    out["billing"] = words.say_top_up(place=place, me="Daimon", foreign=foreign)
    return out
