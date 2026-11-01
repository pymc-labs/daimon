"""Credential cards in the web chat: raised from the MCP process's notice, answered here.

A saved value goes to the agent's vault and never into a message. A saved key carries the
chat on in a follow-up turn only when the agent asked for work to be done with it; a skip,
or a key with no work waiting, only settles the card.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.lifecycle import WebTurnLifecycle
from daimon.core.agent_ownership import owns_agent
from daimon.core.agent_pins import FormPinRefused, request_pin_refusal
from daimon.core.constants import MAX_SECRET_VALUE_BYTES
from daimon.core.credential_submit import apply_env_submit, env_name_refusal, prepare_env_submit
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.env_file import env_name_problem
from daimon.core.operation_policy import TargetFacts, decide_operation
from daimon.core.posted_controls.cards import (
    ALREADY_USED_MESSAGE,
    NO_LONGER_VALID_MESSAGE,
    WRONG_REQUESTER_MESSAGE,
)
from daimon.core.stores import credential_requests as requests_store
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.agent_files import AgentEnvEncryptionRequiredError, agent_env_writes_allowed
from daimon.core.stores.domain import (
    CredentialRequestRow,
    WebCredentialCard,
    WebMessageRow,
    WebThreadRow,
)
from daimon.core.stores.web_notices import WebCredentialNotice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

_EXPIRED = "This form expired. Ask again."
_AGENT_GONE = "This agent no longer exists."
_REPLACE_REFUSED = "Only an admin or the agent's owner can replace {key}. Nothing was saved."
_CHANGED = "{key} changed before you saved. Nothing was saved. Ask again."


class CredentialRefused(Exception):
    """The answer was not taken; the message says why."""


@dataclass(frozen=True)
class CredentialAnswered:
    """An answer taken: the message showing the settled card, and the follow-up turn's
    message when the agent has work waiting on the key."""

    message: WebMessageRow | None
    follow_up: str | None


def card_for(
    row: CredentialRequestRow, *, state: Literal["pending", "saved", "skipped"] = "pending"
) -> WebCredentialCard:
    agent = row.target_name or "This agent"
    return WebCredentialCard(
        id=str(row.idempotency_key),
        state=state,
        headline=f"{agent} needs {row.target}",
        fact=f"Anyone using {agent} can use it. The value isn't shown in chat.",
        key=row.target,
    )


def _thread_uuid(thread_key: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(thread_key.removeprefix("web:"))
    except ValueError:
        return None


class WebCredentialCards:
    """Raises and answers the credential cards of this process's chats."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        anthropic: AsyncAnthropic,
        lifecycle_of: Callable[[uuid.UUID], WebTurnLifecycle | None],
    ) -> None:
        self._sessionmaker = sessionmaker
        self._anthropic = anthropic
        self._lifecycle_of = lifecycle_of

    async def raise_card(self, notice: WebCredentialNotice) -> None:
        """Show the card in its running reply, or on the chat's last reply."""
        async with self._sessionmaker() as session:
            row = await requests_store.get_credential_request_by_idempotency_key(
                session, idempotency_key=notice.request_id
            )
        thread_id = _thread_uuid(notice.thread_id)
        if row is None or row.platform != "web" or row.origin_thread_id != notice.thread_id:
            return
        if thread_id is None:
            return
        card = card_for(row)
        lifecycle = self._lifecycle_of(thread_id)
        if lifecycle is not None and not lifecycle.finished:
            await lifecycle.raise_card(card)
            return
        async with self._sessionmaker.begin() as session:
            reply = await chat_store.get_last_reply(session, thread_id=thread_id)
            if reply is not None and reply.blocks.card is None:
                await chat_store.update_blocks(
                    session,
                    message_id=reply.id,
                    blocks=reply.blocks.model_copy(update={"card": card}),
                )
        log.info("web.credential_card.raised_after_turn")

    async def answer(
        self,
        *,
        thread: WebThreadRow,
        card_id: str,
        clicker: str,
        clicker_platform: str,
        is_admin: bool,
        value: str | None,
    ) -> CredentialAnswered:
        """Save (`value` given) or skip, or raise `CredentialRefused`."""
        row = await self._open_request(thread, card_id, clicker)
        if value is None:
            return CredentialAnswered(await self._skip(row), None)
        message = await self._save(row, value, clicker, clicker_platform, is_admin)
        follow_up = f"[form] {row.target} was saved.\n\n{row.requested_work}"
        return CredentialAnswered(message, follow_up if row.requested_work else None)

    async def _open_request(
        self, thread: WebThreadRow, card_id: str, clicker: str
    ) -> CredentialRequestRow:
        try:
            request_id = uuid.UUID(card_id)
        except ValueError:
            raise CredentialRefused(NO_LONGER_VALID_MESSAGE) from None
        async with self._sessionmaker() as session:
            row = await requests_store.get_credential_request_by_idempotency_key(
                session, idempotency_key=request_id
            )
        if (
            row is None
            or row.platform != "web"
            or row.kind != "env"
            or row.tenant_id != thread.tenant_id
            or row.origin_thread_id != f"web:{thread.id}"
        ):
            raise CredentialRefused(NO_LONGER_VALID_MESSAGE)
        if row.requester_platform_user_id != clicker:
            raise CredentialRefused(WRONG_REQUESTER_MESSAGE)
        if row.used_at is not None:
            raise CredentialRefused(ALREADY_USED_MESSAGE)
        if row.expires_at <= datetime.now(UTC):
            raise CredentialRefused(_EXPIRED)
        return row

    async def _skip(self, row: CredentialRequestRow) -> WebMessageRow | None:
        async with self._sessionmaker.begin() as session:
            spent = await requests_store.consume_credential_request(
                session, token=row.token, now=datetime.now(UTC)
            )
            if spent is None:
                raise CredentialRefused(ALREADY_USED_MESSAGE)
            await requests_store.set_credential_request_outcome(
                session, token=row.token, outcome="declined"
            )
        return await self._settle_card(row, "skipped")

    async def _save(
        self,
        row: CredentialRequestRow,
        value: str,
        clicker: str,
        clicker_platform: str,
        is_admin: bool,
    ) -> WebMessageRow | None:
        if not value.strip():
            raise CredentialRefused("Paste the value first.")
        if len(value.encode()) > MAX_SECRET_VALUE_BYTES:
            raise CredentialRefused(f"That value is too large. Max {MAX_SECRET_VALUE_BYTES} bytes.")
        problem = env_name_problem(row.target, is_admin=is_admin)
        if problem is not None:
            raise CredentialRefused(env_name_refusal(row.target, problem))
        async with self._sessionmaker() as session:
            if not agent_env_writes_allowed(session):
                raise CredentialRefused(str(AgentEnvEncryptionRequiredError()))
        agent = await find_agent_by_derived_uuid(
            self._anthropic, tenant_id=row.tenant_id, agent_id=row.agent_id
        )
        if agent is None:
            raise CredentialRefused(_AGENT_GONE)
        async with self._sessionmaker() as session:
            pinned = await request_pin_refusal(session, row=row, agent=agent)
        if pinned is not None:
            raise CredentialRefused(pinned)

        async def replacement_refused() -> bool:
            return await self._replacement_refused(row, agent, clicker, clicker_platform, is_admin)

        plan = await prepare_env_submit(
            self._sessionmaker, row=row, replacement_refused=replacement_refused
        )
        try:
            result = await apply_env_submit(
                self._sessionmaker,
                row=row,
                agent=agent,
                platform=None,
                value=value,
                plan=plan,
                now=datetime.now(UTC),
            )
        except FormPinRefused as refused:
            raise CredentialRefused(refused.refusal) from None
        if result.consumed is None:
            raise CredentialRefused(ALREADY_USED_MESSAGE)
        log.info("web.credential_card.answered", state=result.state)
        if result.state != "applied":
            await self._settle_card(row, "skipped")
            copy = _REPLACE_REFUSED if result.state == "refused" else _CHANGED
            raise CredentialRefused(copy.format(key=row.target))
        return await self._settle_card(row, "saved")

    async def _replacement_refused(
        self,
        row: CredentialRequestRow,
        agent: BetaManagedAgentsAgent,
        clicker: str,
        clicker_platform: str,
        is_admin: bool,
    ) -> bool:
        """Re-decide a replacement for whoever saves: an admin, or the agent's owner."""
        managed = agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
        owned = False
        if not is_admin and not managed:
            async with self._sessionmaker() as session:
                owned = await owns_agent(
                    session,
                    tenant_id=row.tenant_id,
                    ma_agent_id=str(agent.id),
                    platform=clicker_platform,
                    platform_user_id=clicker,
                )
        facts = TargetFacts(
            is_daimon_managed=managed, is_reachable_in_tenant=False, owned_by_caller=owned
        )
        return decide_operation("key_replace", is_admin=is_admin, target=facts) != "allow"

    async def _settle_card(
        self, row: CredentialRequestRow, state: Literal["saved", "skipped"]
    ) -> WebMessageRow | None:
        """Show the card as answered; the message that carries it."""
        thread_id = _thread_uuid(row.origin_thread_id or "")
        if thread_id is None:
            return None
        card_id = str(row.idempotency_key)
        async with self._sessionmaker.begin() as session:
            message = await chat_store.get_card_message(
                session, thread_id=thread_id, card_id=card_id
            )
            if message is None:
                return None
            blocks = message.blocks.model_copy(update={"card": card_for(row, state=state)})
            await chat_store.update_blocks(session, message_id=message.id, blocks=blocks)
            return message.model_copy(update={"blocks": blocks})
