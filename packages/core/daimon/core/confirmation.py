"""Ask the person who started a turn to approve one action, and wait.

The hook every chat adapter implements so core can pause for a human: a
`ConfirmationPrompt` goes in (what will happen, who may answer, until when),
a `ConfirmationAnswer` comes out. Core builds prompts for gated tool writes
(`prompt_for_tool_call`); a plugin that wants its own review step builds its
own prompt and calls the same hook, so it gets the same card on every
platform for free.

The safe default is `no_confirmation_surface`: an adapter that has no way to
show a card (Teams today, the CLI) answers `denied`, so a gated write is
refused with a plain message rather than run unseen.

`PendingConfirmations` is the in-process rendezvous an adapter uses between
posting a card and its button click: `open` hands out a token and a future,
the click handler `resolve`s the token. A process restart loses the pending
futures, which is fine — the turn waiting on them died with the process.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Final, Literal

from daimon.core.tool_safety import ToolCall
from pydantic import BaseModel, ConfigDict, model_validator

__all__ = [
    "CONFIRMATION_TIMEOUT",
    "ConfirmationAnswer",
    "ConfirmationHook",
    "ConfirmationPrompt",
    "PendingConfirmations",
    "no_confirmation_surface",
    "prompt_for_tool_call",
]

ConfirmationAnswer = Literal["approved", "denied", "expired"]

#: How long a card waits for its button before the action is refused. The
#: session sits idle meanwhile (no model spend), and the per-turn ceiling
#: still bounds the whole turn.
CONFIRMATION_TIMEOUT: Final[timedelta] = timedelta(minutes=10)

#: Cap on the input shown on a card. Enough to read a record's fields; a
#: longer payload is cut with a marker rather than flooding the thread.
MAX_DETAIL_CHARS: Final[int] = 1500


class ConfirmationPrompt(BaseModel):
    """What a confirmation card shows, platform-neutral.

    `fields` are short label/value pairs; `detail` is the exact payload, shown
    verbatim in a code block. Only `requester_platform_user_id` may answer.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    fields: tuple[tuple[str, str], ...] = ()
    detail: str | None = None
    requester_platform_user_id: str
    expires_at: datetime

    @model_validator(mode="after")
    def _aware_expiry(self) -> ConfirmationPrompt:
        if self.expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return self


ConfirmationHook = Callable[[ConfirmationPrompt], Awaitable[ConfirmationAnswer]]


async def no_confirmation_surface(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
    """Default hook for a surface that cannot show a card: refuse."""
    del prompt
    return "denied"


def _render_input(tool_input: dict[str, object]) -> str:
    text = json.dumps(tool_input, indent=2, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) > MAX_DETAIL_CHARS:
        return text[:MAX_DETAIL_CHARS] + "\n… (truncated)"
    return text


def prompt_for_tool_call(
    call: ToolCall, *, requester_platform_user_id: str, now: datetime
) -> ConfirmationPrompt:
    """The card for one gated tool write: server, tool, and the exact input."""
    server = call.server_name or "a tool"
    return ConfirmationPrompt(
        title=f"Approve a write to {server}?",
        fields=(("Tool", call.tool_name), ("Server", server)),
        detail=_render_input(call.input),
        requester_platform_user_id=requester_platform_user_id,
        expires_at=now + CONFIRMATION_TIMEOUT,
    )


class PendingConfirmations:
    """Token → future map between a posted card and its click. No I/O."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[ConfirmationAnswer]] = {}

    def open(self) -> tuple[str, asyncio.Future[ConfirmationAnswer]]:
        token = secrets.token_urlsafe(12)
        future: asyncio.Future[ConfirmationAnswer] = asyncio.get_running_loop().create_future()
        self._pending[token] = future
        return token, future

    def resolve(self, token: str, answer: ConfirmationAnswer) -> bool:
        """Settle `token`. False when it is unknown or already answered."""
        future = self._pending.pop(token, None)
        if future is None or future.done():
            return False
        future.set_result(answer)
        return True

    def discard(self, token: str) -> None:
        self._pending.pop(token, None)

    async def wait(
        self, token: str, future: asyncio.Future[ConfirmationAnswer], *, timeout_s: float
    ) -> ConfirmationAnswer:
        """Wait for the click; `expired` on timeout. Always forgets `token`."""
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
        except TimeoutError:
            return "expired"
        finally:
            self.discard(token)
