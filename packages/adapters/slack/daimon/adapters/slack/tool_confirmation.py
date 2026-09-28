"""Slack's `ConfirmationHook`: post a confirmation card, wait for its button.

The card's words, states and Block Kit come from
`daimon.core.posted_controls.confirmation`; this module posts the card into
the turn's thread, routes the Approve/Deny `block_actions` click back to the
waiting turn through `PendingConfirmations`, and edits the card to the answer.

The registry is in-process, like the cancel registry: the turn waiting on a
card runs in this process, and a restart ends both together.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp
import structlog
from daimon.core.confirmation import (
    ConfirmationAnswer,
    ConfirmationHook,
    ConfirmationPrompt,
    PendingConfirmations,
)
from daimon.core.posted_controls.confirmation import (
    CONFIRMATION_CUSTOM_ID_PREFIX,
    NO_LONGER_PENDING_MESSAGE,
    NOT_YOURS_MESSAGE,
    build_confirmation_blocks,
    build_confirmation_card,
    confirmation_card_text,
    parse_confirmation_custom_id,
)
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.webhook.async_client import AsyncWebhookClient

__all__ = ["CONFIRMATION_CUSTOM_ID_PREFIX", "SlackConfirmationCards"]

log = structlog.get_logger(__name__)

#: Most time a card edit may take.
EDIT_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class _PostedCard:
    prompt: ConfirmationPrompt
    client: AsyncWebClient
    channel: str
    ts: str


class SlackConfirmationCards:
    """Posted confirmation cards awaiting a click, keyed by token."""

    def __init__(self) -> None:
        self._pending = PendingConfirmations()
        self._cards: dict[str, _PostedCard] = {}

    def hook(self, client: AsyncWebClient, *, channel: str, thread_ts: str) -> ConfirmationHook:
        """A hook that posts each prompt's card into `channel`/`thread_ts`."""

        async def _confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
            token, future = self._pending.open()
            card = build_confirmation_card(prompt, state="pending", token=token)
            try:
                response = await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
                    channel=channel,
                    thread_ts=thread_ts,
                    text=confirmation_card_text(card),
                    blocks=build_confirmation_blocks(card, prompt=prompt),
                )
            except SlackApiError:
                self._pending.discard(token)
                raise
            ts = str(response.get("ts") or "")  # pyright: ignore[reportUnknownMemberType]
            self._cards[token] = _PostedCard(prompt=prompt, client=client, channel=channel, ts=ts)
            timeout_s = max(0.0, (prompt.expires_at - datetime.now(UTC)).total_seconds())
            try:
                answer = await self._pending.wait(token, future, timeout_s=timeout_s)
            except BaseException:
                # The turn was stopped while the card was up: retire it.
                posted = self._cards.pop(token, None)
                if posted is not None:
                    await _edit(posted, "denied", answered_by=None)
                raise
            posted = self._cards.pop(token, None)
            if answer == "expired" and posted is not None:
                await _edit(posted, "expired", answered_by=None)
            return answer

        return _confirm

    async def handle_click(self, payload: dict[str, Any]) -> None:
        """Route an Approve/Deny `block_actions` click to its waiting turn."""
        actions: list[dict[str, Any]] = payload.get("actions") or []
        action_id = str(actions[0].get("action_id") or "") if actions else ""
        parsed = parse_confirmation_custom_id(action_id)
        if parsed is None:
            return
        token, answer = parsed
        user: dict[str, Any] = payload.get("user") or {}
        clicker = str(user.get("id") or "")
        posted = self._cards.get(token)
        if posted is None:
            await _ephemeral_from_payload(payload, clicker, NO_LONGER_PENDING_MESSAGE)
            return
        if clicker != posted.prompt.requester_platform_user_id:
            await _ephemeral(posted, clicker, NOT_YOURS_MESSAGE)
            return
        self._cards.pop(token, None)
        if not self._pending.resolve(token, answer):
            await _ephemeral(posted, clicker, NO_LONGER_PENDING_MESSAGE)
            return
        await _edit(posted, answer, answered_by=clicker)


async def _edit(posted: _PostedCard, state: ConfirmationAnswer, *, answered_by: str | None) -> None:
    card = build_confirmation_card(
        posted.prompt, state=state, answered_by_platform_user_id=answered_by
    )
    try:
        # Bounded: this also runs while a turn is being stopped or timed out,
        # and a slow Slack must not hold that up.
        await asyncio.wait_for(
            posted.client.chat_update(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # slack_sdk **kwargs: Unknown
                channel=posted.channel,
                ts=posted.ts,
                text=confirmation_card_text(card),
                blocks=build_confirmation_blocks(card, prompt=posted.prompt),
            ),
            timeout=EDIT_TIMEOUT_S,
        )
    except (SlackApiError, TimeoutError) as err:
        log.warning("slack.tool_confirmation.edit_failed", error=str(err) or type(err).__name__)


async def _ephemeral(posted: _PostedCard, user: str, text: str) -> None:
    try:
        await posted.client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=posted.channel, user=user, text=text
        )
    except SlackApiError as err:
        log.warning("slack.tool_confirmation.ephemeral_failed", error=str(err))


async def _ephemeral_from_payload(payload: dict[str, Any], user: str, text: str) -> None:
    # No card in this process to borrow a client from (answered, expired, or
    # posted before a restart); the click's own response_url still answers.
    response_url = str(payload.get("response_url") or "")
    if not response_url:
        return
    try:
        await AsyncWebhookClient(response_url).send(
            text=text, response_type="ephemeral", replace_original=False
        )
    except aiohttp.ClientError as err:
        log.warning("slack.tool_confirmation.ephemeral_failed", error=str(err), user=user)
