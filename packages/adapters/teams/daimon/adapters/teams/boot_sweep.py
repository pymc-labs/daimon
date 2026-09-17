"""Boot-time orphan-turn retirement for the Teams adapter.

On every process start this lays to rest every Teams turn whose render loop
died with the previous container: it edits the frozen progress message to
the interrupted state and clears the turn marker. It does NOT resume the
turn — the analytical Managed Agents turn may still complete remotely, but
no late answer is delivered and the session row's terminal state is left
untouched. Durable delivery and recovery are downstream follow-up work.

The marker names a Teams message by (service_url, conversation_id,
message_id): ``active_turn_message_id`` carries the message id,
``active_turn_channel_id`` carries the conversation's service_url (the
region-sharded Bot Framework endpoint the conversation lives on), and the
row's ``thread_id`` is the personal conversation id.

Same single-process assumption as Slack's sweep: a marker set by a live
sibling is indistinguishable from one left behind by a crash. The
compare-and-clear below narrows the race but does not remove it — gate on
an owner id before scaling this service out.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import structlog
from daimon.adapters.teams.lifecycle import INTERRUPTED_MESSAGE, terminal_card
from daimon.core.stores.thread_sessions import (
    clear_active_turn_if_message_id,
    list_orphaned_turns,
)
from microsoft_teams.api import (  # pyright: ignore[reportMissingTypeStubs]
    Account,
    ConversationAccount,
    ConversationReference,
    MessageActivityInput,
    SentActivity,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


class TeamsMessageSender(Protocol):
    """The slice of ``ActivitySender`` the sweep needs — a structural type so
    tests and alternative transports satisfy it without the SDK client."""

    async def send(
        self, activity: MessageActivityInput, ref: ConversationReference
    ) -> SentActivity: ...


def _conversation_ref(
    *, service_url: str, conversation_id: str, bot_id: str
) -> ConversationReference:
    return ConversationReference(
        channel_id="msteams",
        service_url=service_url,
        bot=Account(id=bot_id),
        conversation=ConversationAccount(id=conversation_id, conversation_type="personal"),
    )


async def retire_orphaned_turns(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    sender: TeamsMessageSender,
    bot_id: str,
    now: datetime,
) -> None:
    """Edit every orphaned Teams progress message to interrupted, then clear it.

    The read happens ONCE up front and the edit/clear loop is fully
    sequential — the same exposure Slack's sweep documents: a marker written
    between the read and a row's clear is protected by
    ``clear_active_turn_if_message_id``, and the edit targets the message id
    read at sweep start, which always names the frozen message (a new turn
    posts a NEW message at a NEW id before writing its own marker, so the
    held id can never name a live turn's card).

    A row whose service_url is missing, or whose edit fails for any reason —
    deleted message, conversation gone, revoked app registration — is still
    cleared below, so an unreachable card is not retried on every boot
    forever. Clearing is the honest move: the render loop that owned the
    marker is provably dead either way.
    """
    async with sessionmaker() as session:
        orphans = await list_orphaned_turns(session, platform="teams")
    if not orphans:
        return
    log.info("teams.turn.orphans_found", count=len(orphans))

    for row in orphans:
        message_id = row.active_turn_message_id
        if message_id is None:  # list_orphaned_turns filters this; narrow honestly
            continue
        service_url = row.active_turn_channel_id
        if service_url is None:
            # Marker predates the service_url column's use — nothing can
            # address the message, so there is no card to edit. Still clear
            # below so the row is not swept forever.
            log.info("teams.turn.orphan_no_service_url", thread_id=row.thread_id)
        else:
            update = terminal_card(INTERRUPTED_MESSAGE)
            update.id = message_id
            try:
                await sender.send(
                    update,
                    _conversation_ref(
                        service_url=service_url,
                        conversation_id=row.thread_id,
                        bot_id=bot_id,
                    ),
                )
                log.info(
                    "teams.turn.orphan_retired",
                    thread_id=row.thread_id,
                    service_url=service_url,
                    message_id=message_id,
                    # How long the user stared at a spinner — after a crash
                    # this is the only surviving trace, since the turn's own
                    # logs died with its container.
                    frozen_for_s=(
                        (now - row.active_turn_started_at).total_seconds()
                        if row.active_turn_started_at is not None
                        else None
                    ),
                )
            except Exception as err:
                # Broad by design — the SDK's send can fail for any transport
                # or API reason and the row must still be retired below.
                log.warning(
                    "teams.turn.orphan_retire_failed", thread_id=row.thread_id, error=str(err)
                )
        async with sessionmaker() as session:
            cleared = await clear_active_turn_if_message_id(
                session, id=row.id, expected_message_id=message_id
            )
            await session.commit()
        if not cleared:
            # The marker moved between the read and this clear — a live
            # process wrote it after the read, owns the row now, and will
            # clear it on its own terminal path (or crash and be swept next
            # boot). Not a warning: the compare-and-clear working as intended.
            log.info("teams.turn.orphan_marker_moved", thread_id=row.thread_id)
