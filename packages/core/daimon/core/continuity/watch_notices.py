"""Drain owner watch notices from the chat adapter's existing notice poller.

Each send is bracketed by a receipt (`watch_notice_receipts`): the intent is
committed with the claim before the send, and the outcome with the settle
after it. A claim resumed after a crash in between finds the unresolved
intent and hands delivery its time, so delivery looks for the earlier message
before sending again; a claim that finds a sent receipt settles without sending.

Residual window: a second executor that resumes while the first is still mid-send
can miss the first message in the DM; the stable Discord nonce dedups that resend
only within Discord's few-minute nonce window, which the 2-minute claim lease sits in.
"""

from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Protocol

from daimon.core.continuity.watch_imports import UNMAPPED_NOTICE
from daimon.core.stores.domain import WatchRow
from daimon.core.stores.watches import (
    NoticeAttempt,
    SentNotice,
    claim_import_notice,
    close_notice_attempt,
    open_notice_attempt,
    prune_notice_receipts,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DeliverNotice(Protocol):
    """Send `text` to the watch owner; True or a `SentNotice` when it landed."""

    def __call__(
        self, watch: WatchRow, workspace_id: str, text: str, /, *, attempt: NoticeAttempt
    ) -> Awaitable[bool | SentNotice]: ...


async def poll_watch_notices(
    sessions: async_sessionmaker[AsyncSession],
    *,
    deliver: DeliverNotice,
) -> None:
    async with sessions.begin() as db:
        await prune_notice_receipts(db, now=datetime.now(UTC))
    for _ in range(4):
        async with sessions.begin() as db:
            now = datetime.now(UTC)
            notice = await claim_import_notice(db, now=now)
            attempt = None if notice is None else await open_notice_attempt(db, notice[0], now=now)
        if notice is None:
            return
        if attempt is None:
            continue  # An earlier attempt sent it; the claim is already settled.
        watch, workspace_id = notice
        try:
            label = f"Watch {watch.id} ({' '.join(watch.instructions.split())[:80]})"
            reason = str(watch.source_state.get("reason", ""))
            if watch.status == "expired":
                message = f"{label} expired."
            elif reason == "source_unmapped":
                message = UNMAPPED_NOTICE
            elif reason.startswith("Watch paused:"):
                message = f"{label}: {reason}"
            elif reason == "tool_unlisted":
                message = (
                    f"{label} paused: watches here can no longer check that tool.\n"
                    "Ask whoever runs this Daimon to add it back, then ask me to resume it."
                )
            elif reason == "access_revoked":
                message = f"{label} paused: owner access was revoked."
            elif watch.status == "paused" and reason == "failed":
                message = (
                    f"{label} stopped: it could not check its source for a day.\n"
                    "Fix the source, then ask me to resume it."
                )
            elif watch.source_state.get("notice_state") == "daily_cap" and (
                watch.source_state.get("daily_cap_kind") == "owner"
            ):
                message = (
                    f"{label} and your other watches reached your daily limit across all of "
                    "them, so they are waiting before they report again.\n"
                    "Nothing to do: they carry on by themselves within a day."
                )
            elif watch.source_state.get("notice_state") == "daily_cap":
                message = (
                    f"{label} reached its daily limit, so it is waiting before it reports again.\n"
                    "Nothing to do: it carries on by itself within a day."
                )
            else:
                message = f"{label} reached its firing limit: fired {watch.fire_count} times."
            delivered = await deliver(watch, workspace_id, message, attempt=attempt)
        except Exception:
            delivered = False  # Never log owner details or source content.
        if isinstance(delivered, SentNotice):
            sent = delivered
        else:
            sent = SentNotice() if delivered else None
        async with sessions.begin() as db:
            await close_notice_attempt(db, watch, attempt, sent=sent, now=datetime.now(UTC))
