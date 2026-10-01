"""Boot-time retirement of Teams turns the previous process left running.

Managed Agents keeps running (and billing) a turn whose render loop died with
its process. Before admitting a turn, this edits every known card to the
interrupted notice, clears the turn markers, interrupts the orphaned MA
sessions and retires the card intents. No late answer is delivered. Teams bots
cannot read a conversation back, so an intent whose post never returned an id
is retired with a log line, its card (if any) left frozen. Sends use the SDK's
default service URL. Single-process, like Slack's sweep: a live sibling's
marker looks like a crash's leftover, so gate on an owner id before scaling out.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.teams import card
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.core.ma import interrupt_orphaned_session
from daimon.core.stores.thread_sessions import (
    clear_active_turn_if_message_id,
    list_orphaned_turns,
)
from daimon.core.stores.turn_card_intents import (
    list_recoverable_turn_card_intents,
    retire_turn_card_intent,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


async def _interrupt_card(sender: TeamsSender, *, conversation_id: str, message_id: str) -> None:
    notice = card.notice_card(card.INTERRUPTED_NOTICE)
    notice.id = message_id
    try:
        await sender.send(conversation_id, notice, service_url=None)
    except TEAMS_SEND_ERRORS as err:
        # Deleted message, removed bot: the row is retired either way.
        log.warning("teams.turn.orphan_edit_failed", message_id=message_id, error=str(err))


async def retire_orphaned_turns(
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    sender: TeamsSender,
    now: datetime,
) -> None:
    """Interrupt every card, marker and MA turn the previous process left behind."""
    async with sessionmaker() as session:
        orphans = await list_orphaned_turns(session, platform="teams")
        intents = await list_recoverable_turn_card_intents(session, platform="teams")
    if orphans or intents:
        log.info("teams.turn.orphans_found", markers=len(orphans), intents=len(intents))

    edited: set[str] = set()
    for row in orphans:
        message_id = row.active_turn_message_id
        if message_id is None:  # pragma: no cover - list_orphaned_turns filters this out
            continue
        conversation_id = row.active_turn_channel_id or row.thread_id
        await _interrupt_card(sender, conversation_id=conversation_id, message_id=message_id)
        edited.add(message_id)
        async with sessionmaker.begin() as session:
            cleared = await clear_active_turn_if_message_id(
                session, id=row.id, expected_message_id=message_id
            )
        if cleared:
            await interrupt_orphaned_session(anthropic, session_id=row.ma_session_id)
        started = row.active_turn_started_at
        log.info(
            "teams.turn.orphan_retired",
            thread_id=row.thread_id,
            message_id=message_id,
            frozen_for_s=None if started is None else (now - started).total_seconds(),
        )

    for intent in intents:
        if intent.message_id is None:
            log.info("teams.turn_card_intent.unposted", intent_id=str(intent.id))
        elif intent.message_id not in edited:
            await _interrupt_card(
                sender,
                conversation_id=intent.channel_id or intent.thread_id,
                message_id=intent.message_id,
            )
        async with sessionmaker.begin() as session:
            await retire_turn_card_intent(
                session, id=intent.id, expected_message_id=intent.message_id
            )
