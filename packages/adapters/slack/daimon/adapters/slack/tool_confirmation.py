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
import html
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
import structlog
from daimon.adapters.slack.agent_post import post_as_agent
from daimon.core.agent_identity import AgentIdentity
from daimon.core.confirmation import (
    ApprovedConfirmation,
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
from daimon.core.posted_controls.lifecycle import (
    PostedConfirmations,
    check_confirmation_admin,
    edit_card_within,
)
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.webhook.async_client import AsyncWebhookClient

if TYPE_CHECKING:
    from daimon.adapters.slack.runtime import SlackRuntime

__all__ = ["CONFIRMATION_CUSTOM_ID_PREFIX", "SlackConfirmationCards"]

log = structlog.get_logger(__name__)
_COLORS: dict[ConfirmationCardState, str] = {
    "pending": "#FEE75C",
    "approved": "#57F287",
    "denied": "#99AAB5",
    "expired": "#99AAB5",
    "stopped": "#99AAB5",
}


def _plain_message_text(value: str) -> str:
    """Keep Slack control characters literal in fallback and private messages."""
    return html.escape(value, quote=False)


def _attachment(card: ConfirmationCard, prompt: ConfirmationPrompt) -> list[dict[str, Any]]:
    blocks = build_confirmation_blocks(card, prompt=prompt)
    if prompt.admin and card.state == "pending":
        for block in blocks:
            if block["type"] == "actions":
                block["elements"][0]["text"]["text"] = "Confirm"
        blocks.insert(
            1,
            {
                "type": "section",
                "text": {"type": "plain_text", "text": "\n".join(card.detail_lines)},
            },
        )
    return [{"color": _COLORS[card.state], "blocks": blocks}]


#: Most time a card edit may take.
EDIT_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class _PostedCard:
    prompt: ConfirmationPrompt
    client: AsyncWebClient
    channel: str
    ts: str
    thread_ts: str | None
    answered_edit_done: asyncio.Event = field(default_factory=asyncio.Event)
    check_admin: Callable[[ConfirmationPrompt, str], Awaitable[str | None]] | None = None


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
        runtime: SlackRuntime | None = None,
        tenant_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        prepare_admin: Callable[[ConfirmationPrompt], Awaitable[ConfirmationPrompt]] | None = None,
        check_admin: Callable[[ConfirmationPrompt, str], Awaitable[str | None]] | None = None,
    ) -> ConfirmationHook:
        """A hook that posts each prompt's card into `channel`/`thread_ts`."""

        if runtime is not None:
            from daimon.core.channel_backend import backend_selection_enabled, make_backend_service

            if backend_selection_enabled(runtime.settings.turn):
                from daimon.adapters.slack.admin import resolve_is_admin
                from daimon.core.confirmation import backend_switch_callbacks

                if tenant_id is None or account_id is None:
                    raise ValueError("Backend confirmation requires an admitted account and tenant")

                async def current_admin(clicker: str) -> bool:
                    return await resolve_is_admin(client, user_id=clicker)

                prepare_admin, check_admin = backend_switch_callbacks(
                    make_backend_service(runtime.sessionmaker, runtime.settings.turn),
                    sessionmaker=runtime.sessionmaker,
                    anthropic=runtime.anthropic,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    current_admin=current_admin,
                )

        async def _confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer | ApprovedConfirmation:
            if prompt.admin:
                if prepare_admin is None or check_admin is None:
                    return "denied"
                prompt = await prepare_admin(prompt)
            posted_cards: list[_PostedCard] = []

            async def post(token: str) -> _PostedCard:
                card = build_confirmation_card(prompt, state="pending", token=token)
                response = await post_as_agent(
                    client,
                    identity,
                    channel=channel,
                    thread_ts=thread_ts,
                    text=_plain_message_text(confirmation_card_text(card)),
                    attachments=_attachment(card, prompt),
                    mrkdwn=False,
                    parse="none",
                )
                ts = str(response.get("ts") or "")  # pyright: ignore[reportUnknownMemberType]
                if record_post is not None and ts:
                    await record_post(ts)
                parent_thread_ts = (
                    thread_ts
                    if thread_ts and thread_ts != ts and not channel.startswith("D")
                    else None
                )
                posted = _PostedCard(
                    prompt=prompt,
                    client=client,
                    channel=channel,
                    ts=ts,
                    thread_ts=parent_thread_ts,
                    check_admin=check_admin,
                )
                posted_cards.append(posted)
                return posted

            async def retire(posted: _PostedCard, state: ConfirmationCardState) -> None:
                await _edit(posted, state, answered_by=None)

            result = await self._controls.confirm(
                prompt, post=post, retire=retire, post_errors=(SlackApiError,)
            )
            if result == "approved" and posted_cards:
                answered_posted = posted_cards[0]

                async def retire_unsent() -> None:
                    # Queued behind the Approved edit on the same card
                    # (`edit_card_within` keeps per-card call order).
                    await _edit(answered_posted, "stopped", answered_by=None)

                return ApprovedConfirmation(answer="approved", retire_unsent=retire_unsent)
            return result

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
                    _plain_message_text("\n".join(posted.prompt.detail_lines))
                    or "No additional details.",
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
        refusal = await check_confirmation_admin(posted.prompt, clicker, posted.check_admin)
        if refusal is not None:
            await _ephemeral(posted, clicker, refusal)
            return
        if refusal := self._controls.claim(token, clicker, answer):
            if refusal == NOT_YOURS_MESSAGE:
                refusal = refusal.format(requester=f"<@{posted.prompt.requester_platform_user_id}>")
            await _ephemeral(posted, clicker, refusal)
            return
        try:
            await _edit(posted, answer, answered_by=clicker)
        finally:
            posted.answered_edit_done.set()


async def _edit(
    posted: _PostedCard, state: ConfirmationCardState, *, answered_by: str | None
) -> None:
    card = build_confirmation_card(
        posted.prompt, state=state, answered_by_platform_user_id=answered_by
    )
    # Bounded for the turn, finished in the background (`edit_card_within`).
    await edit_card_within(
        posted.client.chat_update(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # slack_sdk **kwargs: Unknown
            channel=posted.channel,
            ts=posted.ts,
            text=_plain_message_text(confirmation_card_text(card)),
            attachments=_attachment(card, posted.prompt),
            mrkdwn=False,
            parse="none",
        ),
        card_key=("slack", posted.channel, posted.ts),
        budget_s=EDIT_TIMEOUT_S,
        failure_errors=(SlackApiError,),
        failed_event="slack.tool_confirmation.edit_failed",
    )


async def _ephemeral(posted: _PostedCard, user: str, text: str) -> None:
    try:
        await posted.client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=posted.channel,
            user=user,
            thread_ts=posted.thread_ts,
            text=text,
            mrkdwn=False,
            parse="none",
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
