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
    UNCLEAR,
    NoticeAttempt,
    SentNotice,
    Unclear,
    claim_import_notice,
    close_notice_attempt,
    open_notice_attempt,
    prune_notice_receipts,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DeliverNotice(Protocol):
    """Send `text` to the watch owner.

    True or a `SentNotice` when it landed, False when it surely did not, and
    `UNCLEAR` when the platform may have stored it (a timeout or server error
    after the request went out).
    """

    def __call__(
        self, watch: WatchRow, workspace_id: str, text: str, /, *, attempt: NoticeAttempt
    ) -> Awaitable[bool | SentNotice | Unclear]: ...


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
            message = notice_text(watch, attempt.kind)
            delivered = await deliver(watch, workspace_id, message, attempt=attempt)
        except Exception:
            # The send may have reached the platform; the next attempt reads the DM first.
            delivered = UNCLEAR  # Never log owner details or source content.
        if isinstance(delivered, SentNotice):
            sent = delivered
        else:
            sent = SentNotice() if delivered is True else None
        async with sessions.begin() as db:
            await close_notice_attempt(
                db,
                watch,
                attempt,
                sent=sent,
                unclear=delivered == UNCLEAR,
                now=datetime.now(UTC),
            )


def notice_text(watch: WatchRow, kind: str) -> str:
    """The owner's wording for each `notice_kind`; the kind is fixed when the notice is claimed."""
    label = f"Watch {watch.id} ({' '.join(watch.instructions.split())[:80]})"
    reason = str(watch.source_state.get("reason", ""))
    if kind == "expired":
        return f"{label} expired."
    if kind == "source_unmapped":
        # The watch id keeps each owner DM distinct, so a resumed send finds its own message.
        return f"{label}: {UNMAPPED_NOTICE}"
    if kind == "paused":
        return f"{label}: {reason}"
    if kind == "tool_unlisted":
        return (
            f"{label} paused: watches here can no longer check that tool.\n"
            "Ask whoever runs this Daimon to add it back, then ask me to resume it."
        )
    if kind == "source_changed":
        return (
            f"{label} paused: the tool it checks changed on its server.\n"
            "Make a new watch if you still want it."
        )
    if kind == "access_revoked":
        return f"{label} paused: owner access was revoked."
    if kind == "failed":
        return (
            f"{label} stopped: it could not check its source for a day.\n"
            "Fix the source, then ask me to resume it."
        )
    if kind == "owner_daily_cap":
        return (
            f"{label} and your other watches reached your daily limit across all of "
            "them, so they are waiting before they report again.\n"
            "Nothing to do: they carry on by themselves within a day."
        )
    if kind == "daily_cap":
        return (
            f"{label} reached its daily limit, so it is waiting before it reports again.\n"
            "Nothing to do: it carries on by itself within a day."
        )
    return f"{label} reached its firing limit: fired {watch.fire_count} times."
