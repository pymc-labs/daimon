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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp
import structlog
from daimon.adapters.slack.agent_post import post_as_agent
from daimon.core.agent_identity import AgentIdentity
from daimon.core.confirmation import (
    ConfirmationAnswer,
    ConfirmationHook,
    ConfirmationPrompt,
)
from daimon.core.posted_controls.confirmation import (
    CONFIRMATION_CUSTOM_ID_PREFIX,
    NOT_YOURS_MESSAGE,
    ConfirmationCard,
    ConfirmationCardState,
    build_confirmation_blocks,
    build_confirmation_card,
    confirmation_card_text,
    parse_confirmation_custom_id,
    parse_details_custom_id,
)
from daimon.core.posted_controls.lifecycle import PostedConfirmations
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.webhook.async_client import AsyncWebhookClient

__all__ = ["CONFIRMATION_CUSTOM_ID_PREFIX", "SlackConfirmationCards"]

log = structlog.get_logger(__name__)
_COLORS: dict[ConfirmationCardState, str] = {
    "pending": "#FEE75C",
    "approved": "#57F287",
    "denied": "#99AAB5",
    "expired": "#99AAB5",
    "stopped": "#99AAB5",
}


def _attachment(card: ConfirmationCard, prompt: ConfirmationPrompt) -> list[dict[str, Any]]:
    return [
        {"color": _COLORS[card.state], "blocks": build_confirmation_blocks(card, prompt=prompt)}
    ]


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
        self._controls = PostedConfirmations[_PostedCard]()

    def hook(
        self,
        client: AsyncWebClient,
        *,
        channel: str,
        thread_ts: str,
        identity: AgentIdentity | None = None,
        record_post: Callable[[str], Awaitable[None]] | None = None,
    ) -> ConfirmationHook:
        """A hook that posts each prompt's card into `channel`/`thread_ts`."""

        async def _confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
            async def post(token: str) -> _PostedCard:
                card = build_confirmation_card(prompt, state="pending", token=token)
                response = await post_as_agent(
                    client,
                    identity,
                    channel=channel,
                    thread_ts=thread_ts,
                    text=confirmation_card_text(card),
                    attachments=_attachment(card, prompt),
                    mrkdwn=False,
                    parse="none",
                )
                ts = str(response.get("ts") or "")  # pyright: ignore[reportUnknownMemberType]
                if record_post is not None and ts:
                    await record_post(ts)
                return _PostedCard(prompt=prompt, client=client, channel=channel, ts=ts)

            async def retire(posted: _PostedCard, state: ConfirmationCardState) -> None:
                await _edit(posted, state, answered_by=None)

            return await self._controls.confirm(
                prompt, post=post, retire=retire, post_errors=(SlackApiError,)
            )

        return _confirm

    async def handle_click(self, payload: dict[str, Any]) -> None:
        """Route an Approve/Deny `block_actions` click to its waiting turn."""
        actions: list[dict[str, Any]] = payload.get("actions") or []
        action_id = str(actions[0].get("action_id") or "") if actions else ""
        if details_token := parse_details_custom_id(action_id):
            posted = self._controls.cards.get(details_token)
            if posted is not None:
                details_user: dict[str, Any] = payload.get("user") or {}
                await _ephemeral(
                    posted,
                    str(details_user.get("id") or ""),
                    "\n".join(posted.prompt.detail_lines) or "No additional details.",
                )
            return
        parsed = parse_confirmation_custom_id(action_id)
        if parsed is None:
            return
        token, answer = parsed
        user: dict[str, Any] = payload.get("user") or {}
        clicker = str(user.get("id") or "")
        posted = self._controls.cards.get(token)
        if posted is None:
            await _ephemeral_from_payload(payload, clicker, self._controls.missing_message(token))
            return
        if refusal := self._controls.claim(token, clicker, answer):
            if refusal == NOT_YOURS_MESSAGE:
                refusal = refusal.format(requester=f"<@{posted.prompt.requester_platform_user_id}>")
            await _ephemeral(posted, clicker, refusal)
            return
        await _edit(posted, answer, answered_by=clicker)


async def _edit(
    posted: _PostedCard, state: ConfirmationCardState, *, answered_by: str | None
) -> None:
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
                attachments=_attachment(card, posted.prompt),
                mrkdwn=False,
                parse="none",
            ),
            timeout=EDIT_TIMEOUT_S,
        )
    except (SlackApiError, TimeoutError) as err:
        log.warning("slack.tool_confirmation.edit_failed", error=str(err) or type(err).__name__)


async def _ephemeral(posted: _PostedCard, user: str, text: str) -> None:
    try:
        await posted.client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=posted.channel, user=user, text=text, mrkdwn=False, parse="none"
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
