"""Teams' `ConfirmationHook`: post a confirmation card, wait for its button.

Draws core's `posted_controls.confirmation` card as an Adaptive Card and routes
the click to the waiting turn through `PendingConfirmations`. The registry is
in-process, like the cancel registry: a restart ends the card and its turn together.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC

import structlog
from daimon.adapters.teams.card_actions import (
    button,
    submitted_fields,
    toast,
)
from daimon.adapters.teams.identity import canonical_uuid
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.core.confirmation import (
    ApprovedConfirmation,
    ConfirmationAnswer,
    ConfirmationHook,
    ConfirmationPrompt,
)
from daimon.core.posted_controls.confirmation import (
    NOT_YOURS_MESSAGE,
    ConfirmationCard,
    ConfirmationCardState,
    build_confirmation_card,
    confirmation_card_text,
)
from daimon.core.posted_controls.lifecycle import (
    PostedConfirmations,
    edit_card_within,
    queue_card_edit,
)
from microsoft_teams.api import (
    AdaptiveCardInvokeActivity,
    AdaptiveCardInvokeResponse,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import (
    ActionSet,
    AdaptiveCard,
    CardElement,
    Container,
    RichTextBlock,
    TextBlock,
    TextRun,
    ToggleVisibilityAction,
)

__all__ = ["VERB", "TeamsConfirmationCards", "confirmation_adaptive_card"]

log = structlog.get_logger(__name__)

VERB = "tool_confirm"
_ANSWERS: dict[str, ConfirmationAnswer] = {"approve": "approved", "deny": "denied"}
#: The click's brief acknowledgement; the card itself updates through its queue.
_ANSWER_TOASTS: dict[ConfirmationAnswer, str] = {
    "approved": "Approved",
    "denied": "Denied",
    "expired": "Expired",
}

#: Most time a card edit may take.
EDIT_TIMEOUT_S = 2.0


def _footer(
    card: ConfirmationCard, prompt: ConfirmationPrompt, answered_by: str | None
) -> tuple[str, ...]:
    name = prompt.requester_display_name or answered_by or "requester"
    if card.state == "pending":
        # Teams renders TIME() in the reader's own timezone.
        expires = prompt.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return (f"Only {name} can approve or deny", f"Expires {{{{TIME({expires})}}}}")
    if card.state in {"approved", "denied"}:
        return (f"by {answered_by or name}",)
    return ()


def confirmation_adaptive_card(
    card: ConfirmationCard, prompt: ConfirmationPrompt, *, answered_by: str | None = None
) -> AdaptiveCard:
    """`card` as an Adaptive Card; buttons only while pending."""
    # Teams parses Markdown in TextBlock. TextRun keeps tool-provided words literal.
    body: list[CardElement] = [
        RichTextBlock(inlines=[TextRun(text=card.headline, weight="Bolder", size="Medium")])
    ]
    if card.body:
        body.append(RichTextBlock(inlines=[TextRun(text=card.body)]))
    if card.consequence:
        body.append(RichTextBlock(inlines=[TextRun(text=card.consequence)]))
    if card.token is not None:
        body.append(Container(items=[], separator=True))
        approve = button(
            VERB,
            "Approve",
            "approve",
            style="positive",
            token=card.token,
        )
        deny = button(
            VERB,
            "Deny",
            "deny",
            style="destructive",
            token=card.token,
        )
        detail_lines = card.detail_lines or ("No additional details.",)
        body.append(
            Container(
                id="approval-details",
                is_visible=False,
                items=[RichTextBlock(inlines=[TextRun(text=line)]) for line in detail_lines],
            )
        )
        body.append(
            ActionSet(
                actions=[
                    approve,
                    deny,
                    ToggleVisibilityAction(title="Details", target_elements=["approval-details"]),
                ]
            )
        )
    for line in _footer(card, prompt, answered_by):
        body.append(TextBlock(text=line, is_subtle=True, size="Small", wrap=True))
    style = (
        "warning" if card.state == "pending" else "good" if card.state == "approved" else "emphasis"
    )
    return AdaptiveCard(
        body=[Container(style=style, items=body)], fallback_text=confirmation_card_text(card)
    )


@dataclass(frozen=True)
class _PostedCard:
    prompt: ConfirmationPrompt
    conversation_id: str
    service_url: str | None
    message_id: str
    answered_edit_done: asyncio.Event = field(default_factory=asyncio.Event)


class TeamsConfirmationCards:
    """Posted confirmation cards awaiting a click, keyed by token."""

    def __init__(self, sender: TeamsSender) -> None:
        self._sender = sender
        self._controls = PostedConfirmations[_PostedCard]()

    def hook(
        self,
        *,
        conversation_id: str,
        service_url: str | None,
        requester_display_name: str | None = None,
    ) -> ConfirmationHook:
        """A hook that posts each prompt's card into the turn's conversation."""

        async def _confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer | ApprovedConfirmation:
            posted_cards: list[_PostedCard] = []
            if requester_display_name:
                prompt = prompt.model_copy(
                    update={"requester_display_name": requester_display_name}
                )

            async def post(token: str) -> _PostedCard:
                card = build_confirmation_card(prompt, state="pending", token=token)
                message = MessageActivityInput().add_card(confirmation_adaptive_card(card, prompt))
                sent = await self._sender.send(conversation_id, message, service_url=service_url)
                posted = _PostedCard(prompt, conversation_id, service_url, sent.id)
                posted_cards.append(posted)
                return posted

            result = await self._controls.confirm(
                prompt, post=post, retire=self._edit, post_errors=TEAMS_SEND_ERRORS
            )
            if result == "approved" and posted_cards:
                answered_posted = posted_cards[0]

                async def retire_unsent() -> None:
                    # Queued behind the Approved edit on the same card
                    # (`edit_card_within` keeps per-card call order).
                    await self._edit(answered_posted, "stopped")

                return ApprovedConfirmation(answer="approved", retire_unsent=retire_unsent)
            return result

        return _confirm

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        """Approve/Deny click: only the requester's first click answers."""
        activity = ctx.activity
        data = submitted_fields(activity.value.action.data)
        token = str(data.get("token") or "")
        answer = _ANSWERS.get(str(data.get("op") or ""))
        posted = self._controls.cards.get(token)
        if posted is None or answer is None:
            return toast(self._controls.missing_message(token))
        clicker = canonical_uuid(activity.from_.aad_object_id)
        if refusal := self._controls.claim(token, clicker, answer):
            if refusal == NOT_YOURS_MESSAGE:
                refusal = refusal.format(
                    requester=posted.prompt.requester_display_name or "requester"
                )
            return toast(refusal)
        card = build_confirmation_card(
            posted.prompt, state=answer, answered_by_platform_user_id=clicker
        )
        name = activity.from_.name
        # The answered card goes through the card's edit queue, registered
        # before this returns, rather than as the invoke response: a delayed
        # response could otherwise land after a Stopped retire that followed.
        queue_card_edit(
            self._send_card(posted, card, answered_by=name),
            card_key=self._card_key(posted),
            failure_errors=TEAMS_SEND_ERRORS,
            failed_event="teams.tool_confirmation.edit_failed",
        )
        posted.answered_edit_done.set()
        return toast(_ANSWER_TOASTS[answer])

    @staticmethod
    def _card_key(posted: _PostedCard) -> object:
        return ("teams", posted.conversation_id, posted.message_id)

    def _send_card(
        self, posted: _PostedCard, card: ConfirmationCard, *, answered_by: str | None = None
    ) -> Awaitable[object]:
        edit = MessageActivityInput(id=posted.message_id).add_card(
            confirmation_adaptive_card(card, posted.prompt, answered_by=answered_by)
        )
        return self._sender.send(posted.conversation_id, edit, service_url=posted.service_url)

    async def _edit(self, posted: _PostedCard, state: ConfirmationCardState) -> None:
        card = build_confirmation_card(posted.prompt, state=state)
        # Bounded for the turn, finished in the background (`edit_card_within`).
        await edit_card_within(
            self._send_card(posted, card),
            card_key=self._card_key(posted),
            budget_s=EDIT_TIMEOUT_S,
            failure_errors=TEAMS_SEND_ERRORS,
            failed_event="teams.tool_confirmation.edit_failed",
        )
