"""In-process confirmation controls, with the existing platform wait policies.

Slack and Teams keep posted cards in a token registry. Discord owns a future
on its live view. Their timeout and cancellation policies intentionally differ.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt, PendingConfirmations
from daimon.core.posted_controls.confirmation import (
    EXPIRED_MESSAGE,
    NO_LONGER_PENDING_MESSAGE,
    NOT_YOURS_MESSAGE,
    ConfirmationCardState,
)


class PromptCard(Protocol):
    @property
    def prompt(self) -> ConfirmationPrompt: ...


def settle_confirmation(
    prompt: ConfirmationPrompt,
    clicker: str | None,
    answer: ConfirmationAnswer,
    resolve: Callable[[ConfirmationAnswer], bool],
) -> str | None:
    """Requester check before the single-use settle; return the original refusal."""
    if clicker != prompt.requester_platform_user_id:
        return NOT_YOURS_MESSAGE
    if datetime.now(UTC) >= prompt.expires_at:
        return EXPIRED_MESSAGE
    if not resolve(answer):
        return NO_LONGER_PENDING_MESSAGE
    return None


class PostedConfirmations[Card: PromptCard]:
    """Token cards share post cleanup, click settlement, expiry and retirement."""

    def __init__(self) -> None:
        self.pending = PendingConfirmations()
        self.cards: dict[str, Card] = {}
        self.expired_tokens: deque[str] = deque(maxlen=512)

    async def confirm(
        self,
        prompt: ConfirmationPrompt,
        *,
        post: Callable[[str], Awaitable[Card]],
        retire: Callable[[Card, ConfirmationCardState], Awaitable[None]],
        post_errors: tuple[type[BaseException], ...],
    ) -> ConfirmationAnswer:
        token, future = self.pending.open()
        try:
            posted = await post(token)
        except post_errors:
            self.pending.discard(token)
            raise
        self.cards[token] = posted
        timeout_s = max(0.0, (prompt.expires_at - datetime.now(UTC)).total_seconds())
        try:
            answer = await self.pending.wait(token, future, timeout_s=timeout_s)
        except BaseException:
            posted = self.cards.pop(token, None)
            if posted is not None:
                await retire(posted, "stopped")
            raise
        posted = self.cards.pop(token, None)
        if answer == "expired" and posted is not None:
            self.expired_tokens.append(token)
            await retire(posted, "expired")
        return answer

    def missing_message(self, token: str) -> str:
        return EXPIRED_MESSAGE if token in self.expired_tokens else NO_LONGER_PENDING_MESSAGE

    def claim(self, token: str, clicker: str | None, answer: ConfirmationAnswer) -> str | None:
        """Keep the card until its requester presses; pop before resolving."""
        posted = self.cards.get(token)
        if posted is None:
            return self.missing_message(token)
        if datetime.now(UTC) >= posted.prompt.expires_at:
            return EXPIRED_MESSAGE

        def resolve(value: ConfirmationAnswer) -> bool:
            self.cards.pop(token, None)
            return self.pending.resolve(token, value)

        return settle_confirmation(posted.prompt, clicker, answer, resolve)


def settle_local_confirmation(
    prompt: ConfirmationPrompt,
    future: asyncio.Future[ConfirmationAnswer] | None,
    clicker: str | None,
    answer: ConfirmationAnswer,
) -> str | None:
    """Discord checks the requester even when its view has already retired."""

    def resolve(value: ConfirmationAnswer) -> bool:
        if future is None or future.done():
            return False
        future.set_result(value)
        return True

    return settle_confirmation(prompt, clicker, answer, resolve)


async def wait_local_confirmation(
    prompt: ConfirmationPrompt,
    future: asyncio.Future[ConfirmationAnswer],
    *,
    stop: Callable[[], None],
    retire: Callable[[ConfirmationCardState], Awaitable[None]],
) -> ConfirmationAnswer:
    """Discord cancels its future and stops its view before a retire edit."""
    timeout_s = max(0.0, (prompt.expires_at - datetime.now(UTC)).total_seconds())
    try:
        answer = await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
    except TimeoutError:
        answer = "expired"
    except asyncio.CancelledError:
        future.cancel()
        stop()
        await retire("stopped")
        raise
    if answer == "expired":
        future.cancel()
        stop()
        await retire("expired")
    return answer
