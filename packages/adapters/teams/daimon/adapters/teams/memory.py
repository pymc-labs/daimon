"""The `memory` command: a read-only look at what the agent where it was typed remembers.

`memory` lists the paths; `memory <path>` shows one memory. Mirrors Slack's
`/memory`, resolved for the place it was typed the way a turn there resolves
its agent: a channel post's thread, or the 1:1 chat. Typed in a channel it is
answered in the 1:1 chat, so a channel whose readers are limited gets a refusal
instead: Slack answers there privately, and Slack's `/dm` likewise refuses to
carry such a channel's work outside it.
"""

from __future__ import annotations

import anthropic
import structlog
from daimon.adapters.teams.card import TEAMS_LIMIT
from daimon.adapters.teams.card_actions import clip, heading
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.errors import DaimonError
from daimon.core.memory_view import (
    get_channel_memory_store,
    get_memory_content,
    list_memory_paths,
)
from daimon.core.permissions import readers_limited_at
from daimon.core.stores.access_policy import load_access_policy
from microsoft_teams.cards import AdaptiveCard, CardElement, TextBlock

log = structlog.get_logger()

EMPTY = "No memory to show here."
KEPT_INSIDE = (
    "This channel's rules keep its agent's memory out of this chat.\n\n"
    "Ask the agent in that channel."
)
_FAILED = "Couldn't load the memory.\n\nTry again in a minute."
_TITLE_MAX_CHARS = 300


def _truncated(text: str) -> str:
    return text if len(text) <= TEAMS_LIMIT else text[:TEAMS_LIMIT] + "\n… (truncated)"


def _card(title: str, text: str | None = None, hint: str | None = None) -> AdaptiveCard:
    title = clip(title, _TITLE_MAX_CHARS)  # A title can carry the path someone typed.
    body: list[CardElement] = [heading(title)]
    if text is not None:
        body.append(TextBlock(text=_truncated(text), font_type="Monospace", wrap=True))
    if hint is not None:
        body.append(TextBlock(text=hint, is_subtle=True, spacing="Medium", wrap=True))
    return AdaptiveCard(body=body, fallback_text=title)


async def _kept_inside(context: CommandContext, asked: TeamsInbound) -> bool:
    """Whether the answer would carry a limited-readers channel's memory to the 1:1 chat."""
    if context.asked_in is None:
        return False
    async with context.runtime.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=context.tenant_id)
    return readers_limited_at(policy, channel_id=asked.channel_id, thread_id=asked.thread_id)


async def _memory_card(context: CommandContext) -> AdaptiveCard:
    runtime, path = context.runtime, context.args
    asked = context.asked_in or context.inbound
    if await _kept_inside(context, asked):
        return _card(KEPT_INSIDE)
    resolved = await get_channel_memory_store(
        runtime.sessionmaker,
        runtime.anthropic,
        tenant_id=context.tenant_id,
        platform="teams",
        user_id=asked.user_id,
        channel_id=asked.channel_id,
        default=runtime.deployment_default,
        # A plain 1:1 chat is its own thread; only a post or a setup conversation is one.
        thread_id=asked.thread_id if asked.thread_id != asked.channel_id else None,
    )
    if resolved is None:
        return _card(EMPTY)
    agent_name, store_id = resolved
    if not path:
        paths = await list_memory_paths(runtime.anthropic, store_id)
        if not paths:
            return _card(EMPTY)
        title = f"{agent_name}'s memory ({len(paths)} files)"
        return _card(title, "\n".join(paths), "Send memory <path> to read one.")
    content = await get_memory_content(runtime.anthropic, store_id, path)
    if content is None:
        return _card(f"No memory file called {path}.", hint="Send memory to see them all.")
    return _card(path, content)


async def show_memory(context: CommandContext) -> None:
    try:
        card = await _memory_card(context)
    except (DaimonError, anthropic.APIError) as exc:
        log.warning("teams.memory.failed", exc_info=exc)
        card = _card(str(exc) if isinstance(exc, DaimonError) else _FAILED)
    await context.send_card(card)
