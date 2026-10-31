"""In-process confirmation controls, with the existing platform wait policies.

Slack and Teams keep posted cards in a token registry. Discord owns a future
on its live view. Their timeout and cancellation policies intentionally differ.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final, Protocol

import structlog
from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt, PendingConfirmations
from daimon.core.posted_controls.confirmation import (
    EXPIRED_MESSAGE,
    NO_LONGER_PENDING_MESSAGE,
    NOT_YOURS_MESSAGE,
    ConfirmationCardState,
)

_log = structlog.get_logger(__name__)

#: Every card edit not yet finished, held so none is collected mid-flight.
_BACKGROUND_EDITS: set[asyncio.Future[object]] = set()
#: The latest edit per card, so edits to one card land in call order.
_LAST_EDIT: dict[object, asyncio.Future[object]] = {}


def pending_card_edits() -> int:
    """Card edits still finishing in the background (for tests and health)."""
    return len(_BACKGROUND_EDITS)


async def drain_card_edits(timeout_s: float) -> int:
    """Wait for queued edits without cancelling any that outlive the deadline."""
    pending = {task for task in _BACKGROUND_EDITS if not task.done()}
    if pending:
        _, pending = await asyncio.wait(pending, timeout=timeout_s)
    return len(pending)


def cancel_pending_card_edits() -> None:
    """Cancel every unfinished card edit; for test isolation only."""
    for task in list(_BACKGROUND_EDITS):
        task.cancel()
    _BACKGROUND_EDITS.clear()
    _LAST_EDIT.clear()


def queue_card_edit(
    edit: Awaitable[object],
    *,
    card_key: object,
    failure_errors: tuple[type[BaseException], ...],
    failed_event: str,
) -> asyncio.Future[object]:
    """Queue a card edit behind the card's previous one, synchronously.

    Edits to one card (`card_key`) run one after another in call order, so a
    slow Approved edit can never land after the Stopped edit that followed it.
    The edit is registered and tracked before this returns, so a caller can
    queue it and return at once (Teams answers a click this way), and
    cancelling a caller neither drops the edit nor hides its failure.
    """
    previous = _LAST_EDIT.get(card_key)

    async def _in_order() -> object:
        if previous is not None and not previous.done():
            # Strict: a later state must never land before an earlier one.
            # Platform clients time out their own calls, so this ends.
            await asyncio.wait({previous})
        return await edit

    task: asyncio.Future[object] = asyncio.ensure_future(_in_order())
    _LAST_EDIT[card_key] = task
    _BACKGROUND_EDITS.add(task)

    def _finished(done: asyncio.Future[object]) -> None:
        _BACKGROUND_EDITS.discard(done)
        if _LAST_EDIT.get(card_key) is done:
            del _LAST_EDIT[card_key]
        if done.cancelled():
            return
        err = done.exception()
        if err is not None:
            _log.warning(failed_event, error=str(err) or type(err).__name__)
            if not isinstance(err, failure_errors):
                _log.error("tool_confirmation.edit_unexpected_error", event_name=failed_event)

    task.add_done_callback(_finished)
    return task


async def edit_card_within(
    edit: Awaitable[object],
    *,
    card_key: object,
    budget_s: float,
    failure_errors: tuple[type[BaseException], ...],
    failed_event: str,
) -> None:
    """Queue a card edit (`queue_card_edit`), waiting at most `budget_s` for it.

    Retiring or answering a card runs while a turn is being stopped or timed
    out, so a slow platform must not hold the turn. Cancelling the edit at the
    budget left the card showing live buttons under load (staging, 2026-10-09:
    two of six expiries timed out at 2s). The edit now completes on its own.
    """
    task = queue_card_edit(
        edit, card_key=card_key, failure_errors=failure_errors, failed_event=failed_event
    )
    done, _ = await asyncio.wait({task}, timeout=budget_s)
    if not done:
        _log.info("tool_confirmation.edit_deferred", edit_event=failed_event, budget_s=budget_s)


class PromptCard(Protocol):
    @property
    def prompt(self) -> ConfirmationPrompt: ...


async def check_confirmation_admin(
    prompt: ConfirmationPrompt,
    clicker: str | None,
    check: Callable[[ConfirmationPrompt, str], Awaitable[str | None]] | None,
    answer: ConfirmationAnswer | None = None,
) -> str | None:
    """Extra opt-in evidence before the existing single-use settlement.

    Requester, expiry and token claiming remain owned by settle_confirmation.
    Existing prompts skip the callback and perform no additional I/O. A Deny
    needs no evidence (nothing changes), so the callback only ever sees a
    Confirm, which it may record (a re-previewed switch card's approval, J5).
    """
    if not prompt.admin or clicker is None or clicker != prompt.requester_platform_user_id:
        return None
    if answer is not None and answer != "approved":
        return None
    refusal = "This change needs a current admin."
    if check is None:
        return refusal
    try:
        return await check(prompt, clicker)
    except Exception:
        _log.warning("tool_confirmation.admin_check_failed")
        return refusal


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


def settle_admin_confirmation(
    prompt: ConfirmationPrompt,
    resolve: Callable[[ConfirmationAnswer], bool],
) -> str | None:
    """An already-authorized admin click competes with every other answer."""
    if datetime.now(UTC) >= prompt.expires_at:
        return EXPIRED_MESSAGE
    if not resolve("approved"):
        return NO_LONGER_PENDING_MESSAGE
    return None


#: A card still open after this long pings its requester once in the thread.
REMIND_AFTER_S: Final[float] = 600.0
#: A reminder is worth sending only with at least this much time left to click.
REMIND_MIN_LEFT_S: Final[float] = 60.0
STILL_WAITING_MESSAGE: Final[str] = "This is still waiting for your click."


async def _remind_once(
    future: asyncio.Future[ConfirmationAnswer],
    expires_at: datetime,
    remind: Callable[[], Awaitable[None]] | None,
) -> None:
    """Wait up to REMIND_AFTER_S; a card still open with a minute or more left pings once.

    The ping is bounded by the card's remaining time, so a slow platform post can
    never hold the card past its expiry.
    """
    timeout_s = (expires_at - datetime.now(UTC)).total_seconds()
    if remind is None or timeout_s - REMIND_AFTER_S < REMIND_MIN_LEFT_S:
        return
    try:
        await asyncio.wait_for(asyncio.shield(future), timeout=REMIND_AFTER_S)
    except TimeoutError:
        left = (expires_at - datetime.now(UTC)).total_seconds()
        if future.done() or left < REMIND_MIN_LEFT_S:
            return
        try:
            await asyncio.wait_for(remind(), timeout=left)
        except Exception as error:  # A missed reminder never ends the wait.
            _log.warning("confirmation.remind_failed", error_type=type(error).__name__)


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
        remind: Callable[[], Awaitable[None]] | None = None,
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
            await _remind_once(future, prompt.expires_at, remind)
            timeout_s = max(0.0, (prompt.expires_at - datetime.now(UTC)).total_seconds())
            answer = await self.pending.wait(token, future, timeout_s=timeout_s)
        except BaseException:
            self.cards.pop(token, None)
            # A winning click pops the token before its waiter resumes. The
            # posted card still needs Stopped if the turn is cancelled then.
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

    def claim_admin(self, token: str) -> str | None:
        """Only call after a fresh admin check; pop before resolving."""
        posted = self.cards.get(token)
        if posted is None:
            return self.missing_message(token)

        def resolve(value: ConfirmationAnswer) -> bool:
            self.cards.pop(token, None)
            return self.pending.resolve(token, value)

        return settle_admin_confirmation(posted.prompt, resolve)


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


def settle_local_admin_confirmation(
    prompt: ConfirmationPrompt, future: asyncio.Future[ConfirmationAnswer] | None
) -> str | None:
    """Discord's already-authorized admin click uses the same future."""

    def resolve(value: ConfirmationAnswer) -> bool:
        if future is None or future.done():
            return False
        future.set_result(value)
        return True

    return settle_admin_confirmation(prompt, resolve)


async def wait_local_confirmation(
    prompt: ConfirmationPrompt,
    future: asyncio.Future[ConfirmationAnswer],
    *,
    stop: Callable[[], None],
    retire: Callable[[ConfirmationCardState], Awaitable[None]],
    remind: Callable[[], Awaitable[None]] | None = None,
) -> ConfirmationAnswer:
    """Discord cancels its future and stops its view before a retire edit."""
    timeout_s = max(0.0, (prompt.expires_at - datetime.now(UTC)).total_seconds())
    try:
        await _remind_once(future, prompt.expires_at, remind)
        timeout_s = max(0.0, (prompt.expires_at - datetime.now(UTC)).total_seconds())
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
