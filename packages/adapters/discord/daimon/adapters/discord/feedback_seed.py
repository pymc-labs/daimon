"""seed_feedback_reactions -- best-effort thumbs-up/down seeding.

Adds the two vote emoji to a turn's final message so a reaction listener
(`feedback_reactions.py`) has something to listen on. Deliberately its own
module importing only `discord`, `structlog`, and the two emoji constants --
no adapter imports -- so `bot.py` can import it at module level with no
cycle, unlike the listener Cog and the button, which both import `DaimonBot`
and therefore need a local import inside `setup_hook`.

`add_reactions` is deliberately NOT added to `permissions.py`'s
`REQUIRED_PERMISSIONS`: seeding is an affordance, never a gate, so a channel
that forbids it must let the turn complete normally rather than change the
bot's advertised required-permission set. `discord.Forbidden` (a subclass of
`discord.HTTPException`) is exactly what a missing-permission channel raises,
and it is caught here and only logged.

This module is therefore a boundary in the sense `guideline:architecture`
means it, despite being three lines of I/O: every caller runs it AFTER the
answer has been delivered and the watermark written, so anything escaping it
reaches the turn's error boundary and posts a "turn failed" message directly
underneath a successfully delivered answer. `discord.HTTPException` is not
the only way `add_reaction` fails -- an `OSError`/`aiohttp` connection error
surviving discord.py's internal retry loop, or a `RuntimeError` from a
closed client session during shutdown drain, both bypass it -- so the catch
is deliberately as wide as the "never raises" contract it implements.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from daimon.core.message_feedback import THUMBS_DOWN, THUMBS_UP
from daimon.core.support_escalation import ESCALATE

import discord

log = structlog.get_logger()


async def seed_feedback_reactions(channel: discord.abc.Messageable, *, message_id: str) -> None:
    """Add the two vote emoji, then the escalate emoji, to `message_id`'s message.

    The positive emoji is added first so the two always render in the same
    order. Best-effort: never raises. `discord.abc.Messageable` itself does
    not declare `get_partial_message` -- only the concrete channel types
    that actually support it do (`discord.Thread` among them, which is what
    every caller in this adapter passes) -- so it is looked up dynamically
    and this is a no-op for a channel type that lacks it.
    """
    get_partial_message: Callable[[int], discord.PartialMessage] | None = getattr(
        channel, "get_partial_message", None
    )
    if get_partial_message is None:
        return
    try:
        partial = get_partial_message(int(message_id))
        await partial.add_reaction(THUMBS_UP)
        await partial.add_reaction(THUMBS_DOWN)
        # The escalate emoji is seeded LAST so the two vote emoji keep the
        # order they have always rendered in. It is not a vote and never
        # reaches message_feedback -- `vote_for_reaction` does not match it.
        await partial.add_reaction(ESCALATE)
    except Exception as exc:  # noqa: BLE001 -- best-effort affordance running after the answer was delivered; a seed failure must never surface as a turn failure (see module docstring)
        log.warning(
            "feedback.seed_failed",
            message_id=message_id,
            err_type=type(exc).__name__,
            error=str(exc),
        )
