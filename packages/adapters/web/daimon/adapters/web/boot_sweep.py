"""Boot-time retirement of web turns the previous process left running.

Managed Agents keeps running (and billing) a turn whose render loop died with its
process. Before serving, this clears the turn markers, interrupts the orphaned sessions,
retires the card intents and marks every reply still running as interrupted. One web
process per deployment: a live sibling's turns would look like leftovers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.web.files import UNREFERENCED_UPLOAD_TTL
from daimon.core.ma import interrupt_orphaned_session
from daimon.core.stores import file_uploads
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores.thread_sessions import (
    clear_active_turn_if_message_id,
    list_orphaned_turns,
    mark_dead,
)
from daimon.core.stores.turn_card_intents import (
    list_recoverable_turn_card_intents,
    retire_turn_card_intent,
)
from mux.contracts.ids import Scope
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: Expired browser sign-ins are deleted this often while the process runs.
SESSION_SWEEP_EVERY_S = 3600.0
#: Web files no message carries are deleted this often while the process runs.
UPLOAD_SWEEP_EVERY_S = 3600.0


async def sweep_orphaned_web_turns(
    anthropic: AsyncAnthropic, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as session:
        orphans = await list_orphaned_turns(session, platform="web")
        intents = await list_recoverable_turn_card_intents(session, platform="web")
    for row in orphans:
        if row.active_turn_message_id is None:  # pragma: no cover - filtered by the query
            continue
        async with sessionmaker.begin() as session:
            cleared = await clear_active_turn_if_message_id(
                session, id=row.id, expected_message_id=row.active_turn_message_id
            )
            if cleared and not await interrupt_orphaned_session(
                anthropic,
                session_id=row.ma_session_id,
                scope=Scope(
                    tenant_id=str(row.tenant_id),
                    account_id=str(row.account_id),
                    principal_id="daimon",
                    authorization_id="web-orphan-sweep",
                )
                if row.account_id is not None
                else None,
            ):
                await mark_dead(session, id=row.id)
    for intent in intents:
        async with sessionmaker.begin() as session:
            await retire_turn_card_intent(
                session, id=intent.id, expected_message_id=intent.message_id
            )
    async with sessionmaker.begin() as session:
        replies = await chat_store.mark_running_interrupted(session)
    if orphans or intents or replies:
        log.info(
            "web.turn.orphans_retired",
            markers=len(orphans),
            intents=len(intents),
            replies=len(replies),
        )


async def sweep_expired_web_sessions(
    sessionmaker: async_sessionmaker[AsyncSession],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Delete the browser sign-ins that have expired; an expired row is never read again."""
    async with sessionmaker.begin() as session:
        removed = await sessions_store.delete_expired(session, now=now())
    if removed:
        log.info("web.session.expired_swept", sessions=removed)
    return removed


async def sweep_expired_web_sessions_forever(
    sessionmaker: async_sessionmaker[AsyncSession], *, every_s: float = SESSION_SWEEP_EVERY_S
) -> None:
    """Sweep now and then every `every_s` seconds, until cancelled."""
    while True:
        try:
            await sweep_expired_web_sessions(sessionmaker)
        except SQLAlchemyError:
            # A database blip must not end the loop; the next pass retries the same rows.
            log.warning("web.session.sweep_failed", exc_info=True)
        await asyncio.sleep(every_s)


async def sweep_stale_web_uploads(
    sessionmaker: async_sessionmaker[AsyncSession],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Delete the web files no message carries after `UNREFERENCED_UPLOAD_TTL`: a file
    uploaded and never sent, or kept for a reply that was never saved."""
    async with sessionmaker.begin() as session:
        removed = await file_uploads.delete_unreferenced_web_uploads(
            session, created_before=now() - UNREFERENCED_UPLOAD_TTL
        )
    if removed:
        log.info("web.upload.stale_swept", files=removed)
    return removed


async def sweep_stale_web_uploads_forever(
    sessionmaker: async_sessionmaker[AsyncSession], *, every_s: float = UPLOAD_SWEEP_EVERY_S
) -> None:
    """Sweep now and then every `every_s` seconds, until cancelled."""
    while True:
        try:
            await sweep_stale_web_uploads(sessionmaker)
        except SQLAlchemyError:
            # A database blip must not end the loop; the next pass retries the same rows.
            log.warning("web.upload.sweep_failed", exc_info=True)
        await asyncio.sleep(every_s)
