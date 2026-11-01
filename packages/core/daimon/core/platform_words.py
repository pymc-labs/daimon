"""What each chat platform calls things, and how a person reaches each action there.

Help cards, refusals, tool results and the per-turn note to the model read this
one table, so a Teams person never sees a Discord command and an action has the
same description everywhere. Static text the model reads (the system prompt,
skills, tool descriptions) stays platform-neutral and points at the per-turn note.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

import structlog
from daimon.core.stores.domain import CHAT_PLATFORMS, ChatPlatform

log = structlog.get_logger()

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
    "cost_footer",
    "privacy",
    "new",
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
        "cost_footer": "Show or hide the cost line under answers (admins)",
        "privacy": "See, export or delete your data",
        "new": "Start a fresh conversation",
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
        return "start a new thread" if place == "channel" else "ask me to start fresh"

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


# Discord registers every command but /privacy and /new as guild-only; /new is DM-only.
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
        "cost_footer": Command("/cost-footer", "space"),
        "privacy": Command("/privacy"),
        "new": Command("/new", "chat"),
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


def one_platform_rule(platform: ChatPlatform | None = None) -> str:
    """The rule that keeps a person from learning Daimon runs on other chat apps.

    Scoped to apps: Daimon still works in every place it has been added on this
    platform, so the model must not deny those. Without a platform (the CLI, a
    headless run) there is no place list."""
    rule = "Never name or hint at another chat app, and never say Daimon works in one."
    if platform is None:
        return rule
    words = WORDS[platform]
    return f"{rule} In {words.label}, Daimon works {words.works_in}."


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


# --- Keeping other chat apps' names out of what Daimon posts -------------------------------

#: What replaces a name; a list of them reads "other apps".
OTHER_APP = "another app"
OTHER_APPS = "other apps"

#: Hosts (the registrable domain, any subdomain too) that give a platform away.
_HOSTS: Mapping[ChatPlatform, str] = MappingProxyType(
    {
        "discord": r"discord(?:app)?\.(?:com|net|gg|media)",
        "slack": r"slack(?:-edge|-files|-imgs)?\.com",
        "teams": r"teams\.(?:microsoft|live)\.com",
    }
)

#: A platform's name as prose. Discord is matched in any case. Slack and Teams only
#: capitalised, so "cut some slack" and "my teams" stay. The cost is a false positive when
#: Slack or Teams opens a sentence in its other sense ("Teams of three work best"): that
#: reads "Another app of three ...", accepted over letting the name through.
_NAMES: Mapping[ChatPlatform, str] = MappingProxyType(
    {
        "discord": r"(?i:discord)",
        "slack": r"(?:Slack(?:bot)?|SLACK)",
        "teams": r"(?:(?i:microsoft)\s+Teams|MS\s+Teams|Teams)",
    }
)

# Letters and digits end a word; an underscore does not, so `discord_send_message` is caught.
_NOT_AFTER_WORD = r"(?<![^\W_])"
_NOT_BEFORE_WORD = r"(?![^\W_])"
# "Discord.com" is a host, not the name followed by a full stop: the URL token takes it.
_NOT_A_HOST = r"(?!\.(?:com|net|gg|media|microsoft|live)\b)"
_SEPARATOR = r"(?P<sep>\s*,\s*(?:(?i:and|or)\s+)?|\s+(?i:and|or)\s+|\s*[/&]\s*)"
# Labels forbid brackets and are bounded, so a stray "[" costs a bounded look, never a rescan.
_LABEL = r"[^\[\]\n]{0,500}"
_TITLE = r"[^)\n]{0,200}"
_SCHEME = r"(?i:[a-z][a-z0-9+.-]{1,15}):"
_TRAILING_PUNCTUATION = r"[^\s<>()\[\].,;:!?'\"*_`~]"
_URL_CHAR = r"[^\s<>()\[\]]"
# A bracketed IPv6 host, and one level of balanced parentheses in a path
# (`/wiki/Foo_(bar)`), stay inside the token instead of splitting it.
_IPV6_HOST = r"(?:\[[0-9A-Fa-f:.]{2,45}\])"
_URL_PARENS = r"(?:\([^\s<>()\[\]]{0,200}\))"
#: Every URL is one opaque token, bare, as a Markdown target or as Slack's `<url|label>`. Only
#: afterwards, from its parsed host, is it replaced whole or kept byte for byte.
_URL_TOKEN = (
    rf"(?P<markdown>!?\[(?P<markdown_label>{_LABEL})\]"
    rf"\(\s*<?(?P<markdown_url>{_SCHEME}//[^\s<>)]+)>?{_TITLE}\))"
    rf"|(?P<angle><(?P<angle_url>{_SCHEME}//[^\s<>|]+)(?:\|(?P<angle_label>[^>\n]{{0,500}}))?>)"
    rf"|(?<![\w+.-])(?P<bare>{_SCHEME}//{_IPV6_HOST}?"
    rf"(?:{_URL_CHAR}|{_URL_PARENS})*(?:{_TRAILING_PUNCTUATION}|{_URL_PARENS}|(?<=\])))"
)


def _name_pattern(platform: ChatPlatform) -> str:
    return rf"{_NOT_AFTER_WORD}{_NAMES[platform]}{_NOT_BEFORE_WORD}{_NOT_A_HOST}"


def _schemeless_host_pattern(hosts: str) -> str:
    """`slack.com/help`, with no scheme: only a host that is another app's can be told apart."""
    return (
        rf"(?<![\w.-])(?P<schemeless>(?i:(?:[a-z0-9-]+\.)*(?:{hosts}))(?::\d+)?"
        rf"(?![\w@-]|:\w|\.\w)(?:[/?#](?:[^\s<>()\[\]]*{_TRAILING_PUNCTUATION})?)?)"
    )


@dataclass(frozen=True, slots=True)
class _Patterns:
    text: re.Pattern[str]
    """Either a URL token or a run of the other apps' names (what gets replaced)."""
    step: re.Pattern[str]
    """One separator and the name after it, to read the separators out of a run."""
    host: re.Pattern[str]
    """A host name that belongs to one of the other apps."""
    domain: re.Pattern[str]
    """One of the other apps' domains anywhere in a string."""


@cache
def _patterns(platform: ChatPlatform) -> _Patterns:
    others: list[ChatPlatform] = [other for other in CHAT_PLATFORMS if other != platform]
    name = "(?:" + "|".join(_name_pattern(other) for other in others) + ")"
    hosts = "|".join(_HOSTS[other] for other in others)
    text = re.compile(
        rf"{_URL_TOKEN}|{_schemeless_host_pattern(hosts)}"
        rf"|(?P<article>{_NOT_AFTER_WORD}(?i:an?|the)\s+)?"
        rf"(?P<first>{name})"
        rf"(?P<more>(?:{_SEPARATOR}{name})*)"
        r"(?P<possessive>['’]s\b)?"
    )
    return _Patterns(
        text=text,
        step=re.compile(_SEPARATOR + name),
        host=re.compile(rf"(?:^|\.)(?:{hosts})$", re.IGNORECASE),
        domain=re.compile(hosts, re.IGNORECASE),
    )


#: How far back a name looks for the end of the sentence before it.
_LOOKBACK = 8


def _opens_sentence(text: str, position: int) -> bool:
    window = text[max(0, position - _LOOKBACK) : position]
    before = window.rstrip(" \t*_>#-")
    if not before:
        return position <= _LOOKBACK  # only the start of the text has nothing before it
    return before.endswith("\n") or (before[-1] in ".!?" and window[-1].isspace())


def _opening(name: str, text: str, position: int) -> str:
    return name[0].upper() + name[1:] if _opens_sentence(text, position) else name


def _plain_name(match: re.Match[str], text: str, step: re.Pattern[str]) -> str:
    """What a run of other apps' names becomes: "another app", or "other apps" for several."""
    separators = [found.group("sep") for found in step.finditer(match.group("more"))]
    several = bool(separators) and not all(re.search(r"\bor\b", sep, re.I) for sep in separators)
    if several:
        name, possessive = OTHER_APPS, "'" if match.group("possessive") else ""
    else:
        name, possessive = OTHER_APP, match.group("possessive") or ""
    return _opening(name, text, match.start()) + possessive


def _host_of(url: str) -> str:
    """The lowercased host `url` really points at, without userinfo, port or trailing dot.

    Raises `ValueError` for a URL that cannot be parsed. A host that is not ASCII is read
    in its IDNA form, so full-width letters do not hide a name."""
    host = (urlsplit(url).hostname or "").rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _is_other_app_url(url: str, patterns: _Patterns) -> bool:
    """Whether `url` is on another app's host; a URL that cannot be parsed is judged by
    whether it contains one of their domains anywhere.

    A parsed host that merely contains one of their domains (`discord.com(Discord)`, which
    `urlsplit` returns as a host, or `discord.com.example.org`) counts too: a false positive
    is a replaced link, a miss is a leak."""
    try:
        host = _host_of(url)
    except ValueError:
        return patterns.domain.search(url) is not None
    return patterns.host.search(host) is not None or patterns.domain.search(host) is not None


def _other_url(match: re.Match[str], text: str, patterns: _Patterns) -> str:
    """A URL token: replaced whole if its host is another app's, else kept byte for byte.

    A kept link still has its visible label scrubbed."""
    if match.group("schemeless") is not None:
        return _opening(OTHER_APP, text, match.start())
    for group, url, label in (
        ("markdown", "markdown_url", "markdown_label"),
        ("angle", "angle_url", "angle_label"),
        ("bare", "bare", None),
    ):
        if match.group(group) is None:
            continue
        if _is_other_app_url(match.group(url), patterns):
            return _opening(OTHER_APP, text, match.start())
        if label is None or match.group(label) is None:
            return match.group(0)
        whole, start = match.group(0), match.start()
        label_start, label_end = match.span(label)
        shown = _scrub(match.group(label), patterns)
        return whole[: label_start - start] + shown + whole[label_end - start :]
    raise AssertionError("a URL token matched no form")  # pragma: no cover


def _scrub(text: str, patterns: _Patterns) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group("first") is not None:
            return _plain_name(match, text, patterns.step)
        return _other_url(match, text, patterns)

    return patterns.text.sub(replace, text)


#: Names the blunt fallback removes; Discord in any case, the others as capitalised.
_BLUNT: Mapping[ChatPlatform, tuple[tuple[str, bool], ...]] = MappingProxyType(
    {
        "discord": (("discord", True),),
        "slack": (
            ("slack.com", True),
            ("slack-edge.com", True),
            ("slack-files.com", True),
            ("slack-imgs.com", True),
            ("Slack", False),
            ("SLACK", False),
        ),
        "teams": (
            ("teams.microsoft.com", True),
            ("teams.live.com", True),
            ("Microsoft Teams", True),
            ("MS Teams", False),
            ("Teams", False),
        ),
    }
)


def _replace_blunt(text: str, needle: str, ignore_case: bool) -> str:
    if not ignore_case:
        return text.replace(needle, OTHER_APP)
    # Lowercase one character at a time so positions in `lowered` are positions in `text`.
    lowered = "".join(c.lower() if len(c.lower()) == 1 else c for c in text)
    needle = needle.lower()
    parts: list[str] = []
    start = 0
    while (found := lowered.find(needle, start)) != -1:
        parts.append(text[start:found])
        start = found + len(needle)
    parts.append(text[start:])
    return OTHER_APP.join(parts)


#: Plain domain substrings for the fallback, which may not use a regex.
_BLUNT_DOMAINS: Mapping[ChatPlatform, tuple[str, ...]] = MappingProxyType(
    {
        "discord": ("discord.com", "discord.gg", "discord.net", "discord.media", "discordapp."),
        "slack": ("slack.com", "slack-edge.com", "slack-files.com", "slack-imgs.com"),
        "teams": ("teams.microsoft.com", "teams.live.com"),
    }
)


def _blunt_token(token: str, others: tuple[ChatPlatform, ...]) -> str:
    """One whitespace-free run: a URL whole or not at all, anything else name by name."""
    if "://" in token:
        folded = unicodedata.normalize("NFKC", token).lower()
        if any(domain in folded for other in others for domain in _BLUNT_DOMAINS[other]):
            return OTHER_APP
        return token
    for other in others:
        for needle, ignore_case in _BLUNT[other]:
            if " " not in needle:
                token = _replace_blunt(token, needle, ignore_case)
    return token


def blunt_scrub_other_platforms(text: str, platform: str | None) -> str:
    """The fallback when the scrub itself fails: replace the other apps' names, nothing smarter.

    Total and regex-free (`str.replace` and `str.find`), so it cannot fail on any input. A
    whitespace-free run holding "://" is a URL: replaced whole if its NFKC-folded text holds
    another app's domain, else kept byte for byte. Elsewhere it ignores word boundaries and
    grammar ("Discordant" becomes "another appant"). Idempotent. A reply posted through it
    is ugly, never lost and never leaking."""
    if platform not in CHAT_PLATFORMS:
        return text
    others: tuple[ChatPlatform, ...] = tuple(other for other in WORDS if other != platform)
    # A name with a space in it ("Microsoft Teams") cannot sit inside a URL, which has none.
    for other in others:
        for needle, ignore_case in _BLUNT[other]:
            if " " in needle:
                text = _replace_blunt(text, needle, ignore_case)
    parts: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = start
        while end < length and not text[end].isspace():
            end += 1
        if end > start:
            parts.append(_blunt_token(text[start:end], others))
        space_end = end
        while space_end < length and text[space_end].isspace():
            space_end += 1
        parts.append(text[end:space_end])
        start = space_end
    return "".join(parts)


def scrub_other_platforms(text: str, platform: str | None) -> str:
    """`text` with the other two chat apps' names replaced by "another app".

    For text Daimon posts on `platform` that a model wrote: a person on one chat app must
    never read Daimon name another. Never touches `platform`'s own name, and returns `text`
    as it is for no platform (the CLI), so a caller need not branch.

    - Discord is matched as a whole word in any case; Slack and Teams only capitalised
      ("Slack", "Microsoft Teams", "Teams"), so the lowercase English "slack", "team" and
      "teams" stay. A sentence that opens with "Slack" or "Teams" in its other sense is
      replaced too, a false positive accepted over a leak.
    - A URL is opaque: it is matched whole first (any scheme, in any case), so the name
      matcher never runs inside one and a working link is never corrupted. Then its parsed,
      lowercased host decides: a URL or link on another app's host (`discord.com`,
      `discord.gg`, `slack.com`, `*.slack.com`, `teams.microsoft.com`, the port, userinfo,
      query and fragment included), bare or as `[text](url)`, `<url>` or Slack's
      `<url|text>`, is replaced whole; any other is kept byte for byte and only a link's
      visible label is scrubbed. A URL that cannot be parsed is replaced whole if it holds
      one of those domains, else kept. A scheme-less `slack.com/help` is replaced too.
      Accepted residuals: a bare URL on another host whose path names an app
      (`github.com/discord/...`) stays as written, and a scheme-less link to another host is
      not recognised as a URL.
    - A run of names reads as one name: "Discord or Slack" is "another app", "Discord and
      Slack" and "Discord, Slack" are "other apps". A leading "a" or "the" goes with it
      ("a Slack channel" is "another app channel").
    - Code is scrubbed too. A fenced block that names Discord is still a mention, and
      skipping code would need a Markdown parser that a streamed, half-written reply
      cannot give.

    Idempotent, linear in the length of `text` (labels, titles and the sentence look-back
    are bounded), and the identity when nothing matches. It never raises: on an unexpected
    error it logs `outbound_scrub.failed` (the exception type only, never the text) and
    falls back to `blunt_scrub_other_platforms`, so a reply is neither lost nor leaked.
    """
    if platform not in CHAT_PLATFORMS:
        return text
    try:
        return _scrub(text, _patterns(platform))
    except Exception as err:
        log.error("outbound_scrub.failed", error_type=type(err).__name__)
        return blunt_scrub_other_platforms(text, platform)
