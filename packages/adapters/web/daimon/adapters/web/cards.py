"""The web chat's `ConfirmationHook`: an approval card ends the reply so far, and its
answer carries the same turn on in a new reply.

Cards wait in this process, like the turns they belong to: a restart ends both.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import structlog
from daimon.adapters.web.lifecycle import WebTurnLifecycle
from daimon.core.authz import Subject
from daimon.core.confirmation import ConfirmationAnswer, ConfirmationHook, ConfirmationPrompt
from daimon.core.posted_controls.confirmation import ConfirmationCardState
from daimon.core.posted_controls.lifecycle import PostedConfirmations
from daimon.core.posted_controls.tool_rule_confirmation import (
    ONLY_ADMIN_MESSAGE,
    RuleScope,
    allowed_rule_scope,
    save_always_allow,
    snapshot_tool_rule,
)
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.domain import WebApprovalCard, WebMessageRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

CardChoice = Literal["approve", "always", "deny"]
_ENDED = {"expired": "Ended without an answer", "stopped": "Stopped"}


@dataclass
class _PostedCard:
    prompt: ConfirmationPrompt
    thread_id: uuid.UUID
    card: WebApprovalCard
    row: WebMessageRow
    rule_scope: RuleScope | None
    subject: Subject

    async def settle(
        self, factory: async_sessionmaker[AsyncSession], card: WebApprovalCard
    ) -> None:
        self.card = card
        async with factory.begin() as session:
            await chat_store.update_blocks(
                session,
                message_id=self.row.id,
                blocks=self.row.blocks.model_copy(update={"card": card}),
            )


class WebCards:
    """Approval cards awaiting their requester, keyed by card id."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker
        self._controls = PostedConfirmations[_PostedCard]()

    def hook(
        self, *, thread_id: uuid.UUID, lifecycle: Callable[[], WebTurnLifecycle], subject: Subject
    ) -> ConfirmationHook:
        """A hook for one turn in `thread_id`; `subject` is the person, for Always allow."""

        async def confirm(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
            if prompt.admin:
                return "denied"  # Admin changes are confirmed in a workspace chat.
            scope: RuleScope | None = None
            if prompt.tool_rule is not None:
                prompt = await snapshot_tool_rule(prompt, self._sessionmaker)
                if prompt.tool_rule is not None:
                    scope = allowed_rule_scope(prompt.tool_rule, subject)

            async def post(token: str) -> _PostedCard:
                card = WebApprovalCard(
                    id=token,
                    state="pending",
                    title=prompt.title,
                    consequence=prompt.consequence,
                    details=prompt.detail_lines,
                    always_allowed=scope is not None,
                )
                row = await lifecycle().end_segment(card)
                return _PostedCard(prompt, thread_id, card, row, scope, subject)

            async def retire(posted: _PostedCard, state: ConfirmationCardState) -> None:
                ended = posted.card.model_copy(
                    update={"state": "denied", "footer": _ENDED.get(state)}
                )
                await posted.settle(self._sessionmaker, ended)

            answer = await self._controls.confirm(prompt, post=post, retire=retire, post_errors=())
            lifecycle().next_segment()
            return answer

        return confirm

    def missing_message(self, card_id: str) -> str:
        return self._controls.missing_message(card_id)

    def open_card(self, thread_id: uuid.UUID) -> str | None:
        return next((t for t, c in self._controls.cards.items() if c.thread_id == thread_id), None)

    async def answer(
        self, *, thread_id: uuid.UUID, card_id: str, clicker: str, choice: CardChoice
    ) -> str | None:
        """Answer an open card; a refusal to show, or None once the turn carries on."""
        posted = self._controls.cards.get(card_id)
        if posted is None or posted.thread_id != thread_id:
            return self._controls.missing_message(card_id)
        if choice == "always" and posted.rule_scope is None:
            return ONLY_ADMIN_MESSAGE
        answer: ConfirmationAnswer = "denied" if choice == "deny" else "approved"
        if refusal := self._controls.claim(card_id, clicker, answer):
            return refusal
        state: Literal["approved", "always", "denied"] = (
            "denied" if choice == "deny" else "approved"
        )
        reference = posted.prompt.tool_rule
        if choice == "always" and reference is not None and posted.rule_scope is not None:
            saved = await save_always_allow(
                self._sessionmaker,
                reference=reference,
                scope=posted.rule_scope,
                subject=posted.subject,
                clicker=clicker,
            )
            if saved == "saved":
                state = "always"
            else:
                log.info("web.card.always_not_saved", result=saved)
        await posted.settle(self._sessionmaker, posted.card.model_copy(update={"state": state}))
        return None

    async def deny_open(self, thread_id: uuid.UUID, clicker: str) -> None:
        """A stop while a card waits answers it with Deny, so the turn can end."""
        if (card_id := self.open_card(thread_id)) is not None:
            await self.answer(thread_id=thread_id, card_id=card_id, clicker=clicker, choice="deny")

    async def deny_all_open(self) -> None:
        """Answer every open card with Deny for its requester: a restart would otherwise
        wait out each turn that is stuck on one."""
        for card_id, posted in list(self._controls.cards.items()):
            await self.answer(
                thread_id=posted.thread_id,
                card_id=card_id,
                clicker=posted.prompt.requester_platform_user_id,
                choice="deny",
            )
