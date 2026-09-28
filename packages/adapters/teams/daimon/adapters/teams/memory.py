"""The `memory` command: a read-only look at what this chat's agent remembers.

`memory` lists the paths; `memory <path>` shows one memory. Mirrors Slack's
`/memory`, resolved for the 1:1 chat the way a turn there resolves its agent.
"""

from __future__ import annotations

import anthropic
import structlog
from daimon.adapters.teams.card_actions import heading
from daimon.adapters.teams.commands import CommandContext
from daimon.core.errors import DaimonError
from daimon.core.memory_view import (
    get_channel_memory_store,
    get_memory_content,
    list_memory_paths,
)
from microsoft_teams.cards import AdaptiveCard, CardElement, TextBlock

log = structlog.get_logger()

EMPTY = "This agent has no memories yet — it will start remembering as it works."
_FAILED = "Something went wrong fetching memory — try again later."
# Headroom under Teams' ~28 KB message limit once the card JSON is added.
_LIMIT = 20_000


def _truncated(text: str) -> str:
    return text if len(text) <= _LIMIT else text[:_LIMIT] + "\n… (truncated)"


def _card(title: str, text: str | None = None, hint: str | None = None) -> AdaptiveCard:
    body: list[CardElement] = [heading(title)]
    if text is not None:
        body.append(TextBlock(text=_truncated(text), font_type="Monospace", wrap=True))
    if hint is not None:
        body.append(TextBlock(text=hint, is_subtle=True, wrap=True))
    return AdaptiveCard(body=body, fallback_text=title)


async def _memory_card(context: CommandContext) -> AdaptiveCard:
    runtime, path = context.runtime, context.args
    resolved = await get_channel_memory_store(
        runtime.sessionmaker,
        runtime.anthropic,
        tenant_id=context.tenant_id,
        platform="teams",
        user_id=context.inbound.user_id,
        channel_id=context.inbound.channel_id,
        default=runtime.deployment_default,
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
        return _card(f"No memory at {path}.", hint="Send memory to list paths.")
    return _card(path, content)


async def show_memory(context: CommandContext) -> None:
    try:
        card = await _memory_card(context)
    except (DaimonError, anthropic.APIError) as exc:
        log.warning("teams.memory.failed", exc_info=exc)
        card = _card(str(exc) if isinstance(exc, DaimonError) else _FAILED)
    await context.send_card(card)
