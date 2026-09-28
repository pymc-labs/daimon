"""Teams' `ConfirmationHook`: post a confirmation card, wait for its button.

Draws core's `posted_controls.confirmation` card as an Adaptive Card and routes
the click to the waiting turn through `PendingConfirmations`. The registry is
in-process, like the cancel registry: a restart ends the card and its turn together.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from daimon.adapters.teams.card_actions import (
    button,
    heading,
    replace_card,
    submitted_fields,
    toast,
)
from daimon.adapters.teams.identity import canonical_uuid
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.core.confirmation import (
    ConfirmationAnswer,
    ConfirmationHook,
    ConfirmationPrompt,
    PendingConfirmations,
)
from daimon.core.posted_controls.confirmation import (
    NO_LONGER_PENDING_MESSAGE,
    NOT_YOURS_MESSAGE,
    ConfirmationCard,
    build_confirmation_card,
    confirmation_card_text,
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
    CodeBlock,
    Fact,
    FactSet,
    TextBlock,
)

__all__ = ["VERB", "TeamsConfirmationCards", "confirmation_adaptive_card"]

log = structlog.get_logger(__name__)

VERB = "tool_confirm"
_ANSWERS: dict[str, ConfirmationAnswer] = {"approve": "approved", "deny": "denied"}

#: Most time a card edit may take.
EDIT_TIMEOUT_S = 2.0


def _footer(card: ConfirmationCard, prompt: ConfirmationPrompt, answered_by: str | None) -> str:
    # Core's footers name people as `<@id>`, which Teams draws literally.
    if card.state == "pending":
        # Teams renders TIME() in the reader's own timezone.
        expires = prompt.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"Only the person who asked can answer. Expires {{{{TIME({expires})}}}}."
    if card.state == "approved" and answered_by:
        return f"Approved by {answered_by}."
    return ""


def confirmation_adaptive_card(
    card: ConfirmationCard, prompt: ConfirmationPrompt, *, answered_by: str | None = None
) -> AdaptiveCard:
    """`card` as an Adaptive Card; buttons only while pending."""
    body: list[CardElement] = [heading(card.headline)]
    if card.fields:
        body.append(FactSet(facts=[Fact(title=label, value=value) for label, value in card.fields]))
    if card.detail:
        body.append(CodeBlock(code_snippet=card.detail, language="Json"))
    if card.token is not None:
        approve = button(VERB, "Approve", "approve", style="positive", token=card.token)
        deny = button(VERB, "Deny", "deny", style="destructive", token=card.token)
        body.append(ActionSet(actions=[approve, deny]))
    if footer := _footer(card, prompt, answered_by):
        body.append(TextBlock(text=footer, is_subtle=True, size="Small", wrap=True))
    return AdaptiveCard(body=body, fallback_text=confirmation_card_text(card))


@dataclass(frozen=True)
class _PostedCard:
    prompt: ConfirmationPrompt
    conversation_id: str
    service_url: str | None
    message_id: str


class TeamsConfirmationCards:
    """Posted confirmation cards awaiting a click, keyed by token."""

    def __init__(self, sender: TeamsSender) -> None:
        self._sender = sender
        self._pending = PendingConfirmations()
        self._cards: dict[str, _PostedCard] = {}

    def hook(self, *, conversation_id: str, service_url: str | None) -> ConfirmationHook:
        """A hook that posts each prompt's card into the turn's conversation."""

        async def _confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
            token, future = self._pending.open()
            card = build_confirmation_card(prompt, state="pending", token=token)
            message = MessageActivityInput().add_card(confirmation_adaptive_card(card, prompt))
            try:
                sent = await self._sender.send(conversation_id, message, service_url=service_url)
            except TEAMS_SEND_ERRORS:
                self._pending.discard(token)
                raise
            self._cards[token] = _PostedCard(prompt, conversation_id, service_url, sent.id)
            timeout_s = max(0.0, (prompt.expires_at - datetime.now(UTC)).total_seconds())
            try:
                answer = await self._pending.wait(token, future, timeout_s=timeout_s)
            except BaseException:
                # The turn was stopped while the card was up: retire it.
                if (posted := self._cards.pop(token, None)) is not None:
                    await self._edit(posted, "denied")
                raise
            posted = self._cards.pop(token, None)
            if answer == "expired" and posted is not None:
                await self._edit(posted, "expired")
            return answer

        return _confirm

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        """Approve/Deny click: only the requester's first click answers."""
        activity = ctx.activity
        data = submitted_fields(activity.value.action.data)
        token = str(data.get("token") or "")
        answer = _ANSWERS.get(str(data.get("op") or ""))
        posted = self._cards.get(token)
        if posted is None or answer is None:
            return toast(NO_LONGER_PENDING_MESSAGE)
        clicker = canonical_uuid(activity.from_.aad_object_id)
        if clicker != posted.prompt.requester_platform_user_id:
            return toast(NOT_YOURS_MESSAGE)
        self._cards.pop(token, None)
        if not self._pending.resolve(token, answer):
            return toast(NO_LONGER_PENDING_MESSAGE)
        card = build_confirmation_card(
            posted.prompt, state=answer, answered_by_platform_user_id=clicker
        )
        name = activity.from_.name
        return replace_card(confirmation_adaptive_card(card, posted.prompt, answered_by=name))

    async def _edit(self, posted: _PostedCard, state: ConfirmationAnswer) -> None:
        card = build_confirmation_card(posted.prompt, state=state)
        edit = MessageActivityInput(id=posted.message_id).add_card(
            confirmation_adaptive_card(card, posted.prompt)
        )
        try:
            # Bounded: this also runs while a turn is being stopped or timed
            # out, and a slow Teams must not hold that up.
            await asyncio.wait_for(
                self._sender.send(posted.conversation_id, edit, service_url=posted.service_url),
                EDIT_TIMEOUT_S,
            )
        except TEAMS_SEND_ERRORS as err:
            log.warning("teams.tool_confirmation.edit_failed", error=str(err) or type(err).__name__)
