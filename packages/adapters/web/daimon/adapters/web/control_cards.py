"""Cards Daimon answers itself in the web chat: a host call's Approve/Deny, a switch's Confirm.

Raised from the MCP process's notice and pressed here, through the same core presses
as Discord, Slack and Teams (requester, message id, tenant, admin recheck). An approved
call has no wake poller on the web: the press's own reply is the wake turn.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import structlog
from daimon.adapters.web.cards import CardChoice
from daimon.adapters.web.lifecycle import WebTurnLifecycle
from daimon.core import host_calls, switch_cards
from daimon.core.config import Settings
from daimon.core.features.snapshot import deployment_on
from daimon.core.stores import host_call_cards
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.domain import WebApprovalCard, WebCard, WebMessageRow, WebThreadRow
from daimon.core.stores.web_notices import WebControlNotice
from daimon.core.turn.deps import TurnDeps
from daimon.core.web_identity import thread_key
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

PREFIXES: Final = ("hcc:", "bsw:")
NOT_YOURS: Final = "Only the person who asked can answer this."
UNAVAILABLE: Final = "This card is unavailable. Ask again to redo it."


class ControlRefused(Exception):
    """The press closed nothing; the message says why."""


@dataclass(frozen=True)
class ControlAnswered:
    """A closing press: the message showing the settled card, and the agent whose wake
    turn runs the approved call (None when nothing waits)."""

    message: WebMessageRow | None
    wake_agent: str | None


def is_control_card(card_id: str) -> bool:
    return card_id.startswith(PREFIXES)


def _thread_uuid(thread_key: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(thread_key.removeprefix("web:"))
    except ValueError:
        return None


async def show_card(
    sessionmaker: async_sessionmaker[AsyncSession],
    lifecycle_of: Callable[[uuid.UUID], WebTurnLifecycle | None],
    thread_id: uuid.UUID,
    card: WebCard,
) -> None:
    """Show `card` in the thread's running reply, or on its last reply."""
    lifecycle = lifecycle_of(thread_id)
    if lifecycle is not None and not lifecycle.finished:
        await lifecycle.raise_card(card)
        return
    async with sessionmaker.begin() as session:
        reply = await chat_store.get_last_reply(session, thread_id=thread_id)
        if reply is not None and reply.blocks.card is None:
            await chat_store.update_blocks(
                session, message_id=reply.id, blocks=reply.blocks.model_copy(update={"card": card})
            )


class WebControlCards:
    """Raises and answers this process's call and switch cards."""

    def __init__(
        self,
        settings: Settings,
        deps: TurnDeps,
        lifecycle_of: Callable[[uuid.UUID], WebTurnLifecycle | None],
    ) -> None:
        self._settings = settings
        self._deps = deps
        self._lifecycle_of = lifecycle_of

    async def raise_card(self, notice: WebControlNotice) -> None:
        thread_id = _thread_uuid(notice.thread_id)
        if thread_id is None or not is_control_card(notice.card_id):
            return
        card = WebApprovalCard(
            id=notice.card_id,
            state="pending",
            title=notice.title,
            details=notice.details,
            footer=notice.footer,
        )
        await show_card(self._deps.sessionmaker, self._lifecycle_of, thread_id, card)
        log.info("web.control_card.raised")

    async def answer(
        self,
        *,
        thread: WebThreadRow,
        card_id: str,
        clicker: str,
        is_admin: bool,
        choice: CardChoice,
    ) -> ControlAnswered:
        """Press the card for `clicker`; raises `ControlRefused` when it closes nothing."""
        try:
            kind, raw = card_id.split(":", 1)
            record = uuid.UUID(raw)
        except ValueError:
            raise ControlRefused(UNAVAILABLE) from None
        if choice == "always":
            raise ControlRefused(UNAVAILABLE)

        async def current_admin(user_id: str) -> bool:
            # The web session's role, read for this request.
            return user_id == clicker and is_admin

        now = datetime.now(UTC)
        deps = self._deps
        approved = choice == "approve"
        wake_agent: str | None = None
        if kind == "hcc":
            async with deps.sessionmaker() as session:
                row = await host_call_cards.get_card(
                    session, tenant_id=thread.tenant_id, card_id=record
                )
            if row is None or row.thread_id != thread_key(thread.id):
                # Pressed from another chat: its wake would run in the wrong thread.
                raise ControlRefused(NOT_YOURS)
            press = await host_calls.press_host_call(
                sessionmaker=deps.sessionmaker,
                # Off is a rollback: presses on cards already posted are refused too.
                enabled=deployment_on(self._settings, "host_call_gate", "web"),
                tenant_id=thread.tenant_id,
                card_id=record,
                action="approve" if approved else "deny",
                clicker=clicker,
                message_id=card_id,
                current_admin=current_admin,
                now=now,
                queue_wake=False,
            )
            if press is None or not press.closed:
                raise ControlRefused(NOT_YOURS if press is None else press.text)
            text, settled = press.text, press.approved
            wake_agent = row.executing_ma_agent_id if press.approved else None
        else:
            pressed = await switch_cards.press_switch_card(
                deps=deps,
                sessionmaker=deps.sessionmaker,
                anthropic=deps.anthropic,
                turn_settings=self._settings.turn,
                deployment_default=deps.deployment_default,
                tenant_id=thread.tenant_id,
                card_id=record,
                action="confirm" if approved else "cancel",
                clicker=clicker,
                current_admin=current_admin,
                place_word="workspace",
                now=now,
                message_id=card_id,
            )
            if pressed is None or not pressed.closed:
                raise ControlRefused(NOT_YOURS if pressed is None else pressed.text)
            text, settled = pressed.text, pressed.applied
        log.info("web.control_card.answered", kind=kind, settled=settled)
        message = await self._settle(thread.id, card_id, text, approved=settled)
        return ControlAnswered(message=message, wake_agent=wake_agent)

    async def _settle(
        self, thread_id: uuid.UUID, card_id: str, text: str, *, approved: bool
    ) -> WebMessageRow | None:
        """Show the card as answered, with what happened in its footer."""
        async with self._deps.sessionmaker.begin() as session:
            message = await chat_store.get_card_message(
                session, thread_id=thread_id, card_id=card_id
            )
            card = None if message is None else message.blocks.card
            if message is None or not isinstance(card, WebApprovalCard):
                return None
            settled = card.model_copy(
                update={"state": "approved" if approved else "denied", "footer": text}
            )
            blocks = message.blocks.model_copy(update={"card": settled})
            await chat_store.update_blocks(session, message_id=message.id, blocks=blocks)
            return message.model_copy(update={"blocks": blocks})
