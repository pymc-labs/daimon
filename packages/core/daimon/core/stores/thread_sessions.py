"""thread_sessions store — newest-row-wins + keep-dead-rows model.

A Discord thread maps to ONE persisted MA session per caller. Multiple rows may
exist for the same (tenant_id, platform, thread_id, account_id) 4-tuple: the
live lookup returns the newest live row (ORDER BY created_at DESC LIMIT 1) that
belongs to the requesting caller's account. Dead rows are kept as audit so the
recreate-on-4xx path can insert a second row without losing the history of prior
sessions.

Security invariant: get_live_thread_session filters by account_id equality —
NULL rows (frozen pre-migration rows) never match any non-null caller, so every
existing thread cold-creates a fresh per-caller session on the next turn.

No unique constraint on thread identity — recreate intentionally inserts a
second row while marking the old row 'dead'.

Two read helpers exist for deliberately different purposes:
`get_live_thread_session` is the turn pipeline's caller-scoped lookup (see the
security invariant above); `get_latest_thread_session` drops the account_id
predicate and exists only to give message feedback an attribution hint.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime
from typing import Any, cast

from daimon.core._models import ThreadSession
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.stores.domain import ThreadSessionRow, TransferKind, UnsavedWorkChoice
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def get_live_thread_session(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    platform: str,
    thread_id: str,
    account_id: _uuid.UUID,
) -> ThreadSessionRow | None:
    """Return the newest live row for (tenant_id, platform, thread_id, account_id), or None.

    The account_id filter is a plain equality predicate — no OR IS NULL branch.
    Rows with account_id NULL (pre-migration frozen rows) never match any live
    caller, so those threads cold-create a fresh per-caller session on the next
    turn (security guard).
    """
    orm = (
        await session.execute(
            select(ThreadSession)
            .where(
                ThreadSession.tenant_id == tenant_id,
                ThreadSession.platform == platform,
                ThreadSession.thread_id == thread_id,
                ThreadSession.account_id == account_id,
                ThreadSession.status == "live",
            )
            .order_by(ThreadSession.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if orm is None:
        return None
    return ThreadSessionRow.model_validate(orm)


async def get_latest_thread_session(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    platform: str,
    thread_id: str,
) -> ThreadSessionRow | None:
    """Return the newest live row for (tenant_id, platform, thread_id), or None.

    Deliberately NOT the session-lookup used by the turn pipeline — dropping
    the `account_id` predicate that `get_live_thread_session` applies also
    drops the caller-isolation guarantee that predicate exists to provide.
    This helper exists for attribution hints only (message feedback resolving
    which session likely authored a reacted-to message) and must never be
    used to bind or resume a session.
    """
    orm = (
        await session.execute(
            select(ThreadSession)
            .where(
                ThreadSession.tenant_id == tenant_id,
                ThreadSession.platform == platform,
                ThreadSession.thread_id == thread_id,
                ThreadSession.status == "live",
            )
            .order_by(ThreadSession.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if orm is None:
        return None
    return ThreadSessionRow.model_validate(orm)


async def create_thread_session(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    platform: str,
    thread_id: str,
    account_id: _uuid.UUID,
    ma_session_id: str,
    ma_agent_id: str | None = None,
    watermark_message_id: str | None = None,
    created_at: datetime | None = None,
    effective_config: SessionSnapshot | None = None,
    identity_fingerprint: str | None = None,
    mutable_fingerprint: str | None = None,
    predecessor_id: _uuid.UUID | None = None,
    transfer_file_id: str | None = None,
    transfer_kind: TransferKind | None = None,
) -> ThreadSessionRow:
    """Insert a new thread-session mapping row and return the Pydantic domain type.

    account_id is the calling user's account and is persisted on the row so that
    get_live_thread_session can scope future lookups to the same caller.

    The optional `created_at` kwarg is provided so tests can control ordering
    deterministically for newest-row-wins assertions. When None, the DB
    server_default (now()) applies.

    `effective_config` is the configuration the freshly created MA session
    froze, with its two fingerprints; omitting it writes a row that reads as
    "configuration unknown", which is exactly what every pre-continuity row is.
    `predecessor_id`, `transfer_file_id` and `transfer_kind` are set only when
    this row replaces an earlier session and carries its work forward.
    """
    kwargs: dict[str, object] = {
        "tenant_id": tenant_id,
        "platform": platform,
        "thread_id": thread_id,
        "account_id": account_id,
        "ma_session_id": ma_session_id,
        "ma_agent_id": ma_agent_id,
        "watermark_message_id": watermark_message_id,
        "effective_config": (
            None if effective_config is None else effective_config.model_dump(mode="json")
        ),
        "identity_fingerprint": identity_fingerprint,
        "mutable_fingerprint": mutable_fingerprint,
        "predecessor_id": predecessor_id,
        "transfer_file_id": transfer_file_id,
        "transfer_kind": transfer_kind,
    }
    if created_at is not None:
        kwargs["created_at"] = created_at
    orm = ThreadSession(**kwargs)
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return ThreadSessionRow.model_validate(orm)


async def update_watermark(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
    watermark_message_id: str,
) -> None:
    """Persist the bot's final reply message id as the watermark for this mapping."""
    await session.execute(
        update(ThreadSession)
        .where(ThreadSession.id == id)
        .values(watermark_message_id=watermark_message_id)
    )
    await session.flush()


async def mark_turn_active(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
    active_turn_message_id: str,
    now: datetime,
    active_turn_channel_id: str | None = None,
) -> None:
    """Record that a turn is running and which message is rendering it.

    Written once the embed exists and the mapping row is known. The pair is
    what lets a restarted process find embeds it can never finish -- the
    process holding them is gone, and nothing else knows they were mid-flight.

    active_turn_channel_id is optional because a Discord message id is
    globally addressable on its own, while a Slack message is identified by
    (channel, ts). A caller that omits it stores NULL, which is the correct
    reading for Discord.
    """
    await session.execute(
        update(ThreadSession)
        .where(ThreadSession.id == id)
        .values(
            active_turn_message_id=active_turn_message_id,
            active_turn_started_at=now,
            active_turn_channel_id=active_turn_channel_id,
        )
    )
    await session.flush()


async def clear_active_turn(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
) -> None:
    """Clear the in-flight marker. Call on every terminal path, including failure.

    All three marker columns are cleared together so a cleared row carries no
    dead channel id.
    """
    await session.execute(
        update(ThreadSession)
        .where(ThreadSession.id == id)
        .values(
            active_turn_message_id=None,
            active_turn_started_at=None,
            active_turn_channel_id=None,
        )
    )
    await session.flush()


async def clear_active_turn_if_message_id(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
    expected_message_id: str,
) -> bool:
    """Clear the in-flight marker only if it still names the message the caller
    read earlier. Returns whether it cleared.

    For a reader that snapshotted the marker at some point in the past -- the
    boot sweep -- and must not clobber a marker written since that snapshot.
    `clear_active_turn` above stays unconditional and is still the right call
    for callers that OWN the row: a turn's own terminal path wrote the marker
    itself and is entitled to clear it outright, no comparison needed.

    A `False` return is safe, not merely tolerated: the marker no longer
    matching means a live process wrote a new one after the caller's read, and
    that process will clear it on its own terminal path -- or crash, in which
    case the next boot's sweep finds it. A skip can never strand a row. A NULL
    marker never equals a string either, so a row that was already cleared
    also returns `False` rather than raising.

    The id predicate confines the write to the one row the caller named, so
    two rows that happen to carry the same message id cannot be crossed.
    """
    result = await session.execute(
        update(ThreadSession)
        .where(
            ThreadSession.id == id,
            ThreadSession.active_turn_message_id == expected_message_id,
        )
        .values(
            active_turn_message_id=None,
            active_turn_started_at=None,
            active_turn_channel_id=None,
        )
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount == 1


async def list_orphaned_turns(
    session: AsyncSession,
    *,
    platform: str,
) -> list[ThreadSessionRow]:
    """Rows still flagged as mid-turn, i.e. turns no live process owns.

    Only meaningful at process start: a turn cannot outlive the process
    rendering it, so any marker still set when we boot belongs to a turn that
    died with the previous container.

    ponytail: assumes one adapter process per platform. With two, this would
    reap the other's in-flight turns -- and the boot sweeps now also send each
    reaped row's MA session a `user.interrupt`, so they would stop those live
    turns outright. Gate on an owner id before scaling out.
    """
    rows = (
        await session.execute(
            select(ThreadSession).where(
                ThreadSession.platform == platform,
                ThreadSession.active_turn_message_id.is_not(None),
            )
        )
    ).scalars()
    return [ThreadSessionRow.model_validate(row) for row in rows]


async def mark_dead(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
) -> None:
    """Mark a mapping row as dead (session expired or recreated).

    The row is retained in the table as an audit trail; it will be excluded
    from future get_live_thread_session lookups.
    """
    await session.execute(update(ThreadSession).where(ThreadSession.id == id).values(status="dead"))
    await session.flush()


async def get_thread_session_by_id(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
) -> ThreadSessionRow | None:
    """Return a mapping row by id regardless of status (live or dead).

    Unlike `get_live_thread_session`, this does not filter on `status` -- it
    exists for callers (audit trails, dead-session-recovery assertions) that
    need to read a specific row's current status rather than look up the
    newest live row for a caller.
    """
    orm = (
        await session.execute(select(ThreadSession).where(ThreadSession.id == id))
    ).scalar_one_or_none()
    if orm is None:
        return None
    return ThreadSessionRow.model_validate(orm)


async def update_agent_identity(session: AsyncSession, *, id: _uuid.UUID, ma_agent_id: str) -> None:
    """Record the identity observed on MA without replacing session history."""
    await session.execute(
        update(ThreadSession).where(ThreadSession.id == id).values(ma_agent_id=ma_agent_id)
    )
    await session.flush()


async def record_snapshot(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
    snapshot: SessionSnapshot,
    identity_fingerprint: str,
    mutable_fingerprint: str,
) -> None:
    """Persist the configuration this session is running, with both fingerprints.

    Written on create and again when a legacy row is backfilled from a
    `sessions.retrieve`. Both fingerprints are supplied by the caller rather
    than computed here: the store does not import hashing logic, and a caller
    that compared fingerprints already has them.
    """
    await session.execute(
        update(ThreadSession)
        .where(ThreadSession.id == id)
        .values(
            effective_config=snapshot.model_dump(mode="json"),
            identity_fingerprint=identity_fingerprint,
            mutable_fingerprint=mutable_fingerprint,
        )
    )
    await session.flush()


async def set_pending_unsaved_work(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
    choice: UnsavedWorkChoice,
) -> None:
    """Record what this caller said to do with uncommitted repository changes.

    Written when the answer arrives, read by the replacement it governs, and
    cleared the moment the row stops being live (`mark_superseded` /
    `mark_retired`) so an answer can never be applied to a second, unrelated
    replacement. A later answer overwrites an earlier one: the last thing the
    caller said is the one that holds.
    """
    await session.execute(
        update(ThreadSession).where(ThreadSession.id == id).values(pending_unsaved_work=choice)
    )
    await session.flush()


async def update_mutable_fingerprint(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
    snapshot: SessionSnapshot,
    mutable_fingerprint: str,
) -> None:
    """Record an in-place refresh: new mutable axis, same session, same identity.

    `identity_fingerprint` is deliberately left alone — an in-place update that
    moved the identity axis would be a bug, and overwriting it here would hide
    that the row no longer describes the session MA is running.
    """
    await session.execute(
        update(ThreadSession)
        .where(ThreadSession.id == id)
        .values(
            effective_config=snapshot.model_dump(mode="json"),
            mutable_fingerprint=mutable_fingerprint,
        )
    )
    await session.flush()
