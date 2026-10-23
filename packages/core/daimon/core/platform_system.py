"""The system prompt a session runs on one chat platform.

The system prompt is a field of the agent, which every platform shares, and its
hash (`SessionSnapshot.system_sha256`) decides when a session is replaced. The
shipped prompt covers all three chat platforms, so each would tell the model
about the other two. A Discord, Slack or Teams session therefore runs its own
variant, sent as the session's own `system` override (`agent_with_overrides`) and
hashed the same way (`desired_snapshot`). A platform with no chat (the CLI, a
headless run without one) runs the agent's own prompt.

A variant swaps known passages of the shipped prompt (`defaults/agents/daimon.yaml`
and `agent_guidance`) for wording that names only its platform. It then fails
closed: any line that still names one of the other two platforms, because the
prompt drifted or a tenant edited it, is dropped.
"""

from __future__ import annotations

import re

import structlog
from daimon.core.context_prompt import FRAGMENT_MARKER
from daimon.core.platform_words import one_platform_rule
from daimon.core.stores.domain import CHAT_PLATFORMS, ChatPlatform

log = structlog.get_logger(__name__)

_PLATFORMS = CHAT_PLATFORMS

_EVERY_PLATFORM = (
    "with the same plain words on every chat platform and no dot separators.",
    "with the same plain words and no dot separators.",
)
_FORMAT_INTRO = """\
You run across more than one chat platform. The `<channel>` element in your
context says which: `platform="discord"`, `"slack"` or `"teams"`, plus the
`id` of the channel you are in right now. Format your output for that
platform:
"""
_FORMAT_DISCORD = """\
- Discord: a CommonMark subset — `**bold**`, `*italic*`, `` `code` ``,
  fenced code blocks, `# / ## / ###` headings, `> quote`, `- list`, and
  `[text](url)` links all work.
"""
_FORMAT_SLACK = """\
- Slack: mrkdwn — `*bold*` (single asterisks), `_italic_`, `` `code` ``,
  fenced code blocks, `> quote`, `- list`. Links render as either
  `[text](url)` or `<url|text>`; `#` headings do NOT render.
  To mention a person, write `<@USER_ID>` taking USER_ID from the
  `author_id` attribute on the `<user_query>` element. To link a channel,
  write `<#CHANNEL_ID>` taking CHANNEL_ID from the `id` attribute on the
  `<channel>` element. Both render as clickable names — do NOT print raw
  IDs. Never write `<!channel>`, `<!here>`, or `<!everyone>`.
"""
_FORMAT_TEAMS = """\
- Teams: Markdown — `**bold**`, `*italic*`, `` `code` ``, fenced code
  blocks, `- list` and `[text](url)` links. Tables do not render.
"""
_FORMAT_TABLES = (
    "Final-reply Markdown tables render as images on Discord unless the tenant\n"
    "opts out, and as native tables on Slack unless the tenant opts out. "
    "Otherwise prefer bullets or code blocks. "
    "Use blank lines for separation rather than horizontal rules (`---`)."
)
_SHIPPED_FORMAT = _FORMAT_INTRO + _FORMAT_DISCORD + _FORMAT_SLACK + _FORMAT_TEAMS + _FORMAT_TABLES

_CHANNEL_ID = (
    "The `<channel>` element in your context gives the `id` of the channel you are\nin right now. "
)
_SEPARATION = "Use blank lines for separation rather than horizontal rules (`---`)."
_FORMATTING = {
    "discord": (
        _CHANNEL_ID + "Format your output as Discord renders it:\n"
        "a CommonMark subset — `**bold**`, `*italic*`, `` `code` ``,\n"
        "fenced code blocks, `# / ## / ###` headings, `> quote`, `- list`, and\n"
        "`[text](url)` links all work.\n"
        "Final-reply Markdown tables render as images unless the tenant opts out. "
        "Otherwise prefer bullets or code blocks. " + _SEPARATION
    ),
    "slack": (
        _CHANNEL_ID + "Format your output as Slack mrkdwn: `*bold*` (single asterisks),\n"
        "`_italic_`, `` `code` ``, fenced code blocks, `> quote`, `- list`. Links render\n"
        "as either `[text](url)` or `<url|text>`; `#` headings do NOT render.\n"
        "To mention a person, write `<@USER_ID>` taking USER_ID from the\n"
        "`author_id` attribute on the `<user_query>` element. To link a channel,\n"
        "write `<#CHANNEL_ID>` taking CHANNEL_ID from the `id` attribute on the\n"
        "`<channel>` element. Both render as clickable names — do NOT print raw\n"
        "IDs. Never write `<!channel>`, `<!here>`, or `<!everyone>`.\n"
        "Final-reply Markdown tables render as native tables unless the tenant opts out. "
        "Otherwise prefer bullets or code blocks. " + _SEPARATION
    ),
    "teams": (
        _CHANNEL_ID + "Format your output as Markdown: `**bold**`, `*italic*`,\n"
        "`` `code` ``, fenced code blocks, `- list` and `[text](url)` links. Tables do\n"
        "not render; prefer bullets or code blocks. " + _SEPARATION
    ),
}

_NEVER_ASSUME = (
    "the channel you are already in, and never\nassume a platform: read it from the "
    "`<channel>` element.",
    "the channel you are already in.",
)

_FILES_SHIPPED = """\
In a chat turn on Discord, Slack or Teams,
every file you save under /mnt/session/outputs is attached to your reply
after your turn, except where a Teams `files` attribute (below) says
`unavailable`."""
#: The `files` attribute exists on Teams only, so only a Teams turn is told of it.
_FILES = {
    "teams": """\
In a chat turn, every file you save under
/mnt/session/outputs is attached to your reply after your turn, except where
the `files` attribute (below) says `unavailable`.""",
    "discord": """\
In a chat turn, every file you save under
/mnt/session/outputs is attached to your reply after your turn.""",
    "slack": """\
In a chat turn, every file you save under
/mnt/session/outputs is attached to your reply after your turn.""",
}
_FILES_ATTRIBUTE_PARAGRAPH = """\
In a Teams channel or group chat, the `files` attribute on `<channel>`
says whether files can be attached. `available`: as above. Never
write file links yourself, even when earlier replies show them. Files you
delivered earlier come back as attachments of your replayed messages, with a
download URL, so you can open them again. `unavailable`: no file can be
attached, so never promise one and save none to /mnt/session/outputs. When
someone asks for a file, say so once, following the channel's `files_hint`;
paste the content inline if it is short text, otherwise suggest a 1:1 chat
with the bot, a separate conversation where the file would be made again. send_message cannot attach
a file there either, so never call it or create_file_upload_url for one.

"""
#: Teams' own wording: a person from another organisation can't open a 1:1 chat, and the
#: turn's `files_hint` says so. Discord and Slack never see this passage.
_FILES_ATTRIBUTE_PARAGRAPH_TEAMS = _FILES_ATTRIBUTE_PARAGRAPH.replace(
    "where the file would be made again.",
    "where the file would be made again, unless the files hint says this person can't open one.",
)

#: (shipped passage, replacement) for one platform. Each shipped passage appears
#: verbatim in the shipped prompt; `test_platform_system` fails when one drifts out.
SHIPPED_PASSAGES: tuple[str, ...] = (
    _EVERY_PLATFORM[0],
    _SHIPPED_FORMAT,
    _NEVER_ASSUME[0],
    _FILES_SHIPPED,
    _FILES_ATTRIBUTE_PARAGRAPH,
)


def _swaps(platform: ChatPlatform) -> tuple[tuple[str, str], ...]:
    return (
        _EVERY_PLATFORM,
        (_SHIPPED_FORMAT, f"{_FORMATTING[platform]}\n\n{one_platform_rule(platform)}"),
        _NEVER_ASSUME,
        (_FILES_SHIPPED, _FILES[platform]),
        (
            _FILES_ATTRIBUTE_PARAGRAPH,
            _FILES_ATTRIBUTE_PARAGRAPH_TEAMS if platform == "teams" else "",
        ),
    )


def _names_another_platform(platform: str) -> re.Pattern[str]:
    others = "|".join(p for p in _PLATFORMS if p != platform)
    return re.compile(rf"\b({others})\b", re.IGNORECASE)


def system_for_platform(system: str | None, platform: str | None) -> str | None:
    """The prompt to send for this platform's session instead of the agent's own.

    None when the agent's own prompt is right: no prompt, no chat platform, or a
    prompt the variant would leave exactly as it is."""
    if system is None or platform not in _PLATFORMS:
        return None
    # Tenant context fragments follow the marker as one JSON line `context_prompt` decodes:
    # they are configuration, not prompt, so neither the swaps nor the line drop touch them.
    prompt, marker, fragments = system.partition(FRAGMENT_MARKER)
    variant = prompt
    for shipped, own in _swaps(platform):
        variant = variant.replace(shipped, own)
    foreign = _names_another_platform(platform)
    lines = variant.split("\n")
    kept = [line for line in lines if not foreign.search(line)]
    if len(kept) != len(lines):
        log.warning("system_prompt.lines_dropped", platform=platform, count=len(lines) - len(kept))
        variant = "\n".join(kept)
    variant += marker + fragments
    return None if variant == system else variant
