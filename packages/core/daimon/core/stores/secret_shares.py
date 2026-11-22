"""Shared secrets: encrypted on write, revealed once to their recipient, then cleared.

Callers own transactions. A value is never logged, returned by a listing or kept after
its reveal or expiry; there is no plaintext fallback (`NO_KEY` without deployment keys).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from daimon.core._models import SecretShare
from daimon.core.errors import StoreError
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.secret_shares import MAX_LIVE_PER_SENDER, NO_KEY, SHARE_LIFETIME, RevealRefusal
from daimon.core.stores.agent_files import _cipher  # pyright: ignore[reportPrivateUsage]
from sqlalchemy import ColumnElement, delete, func, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


class SecretShareEncryptionRequiredError(StoreError):
    """No deployment keys: a shared secret is never stored in plaintext."""

    def __init__(self) -> None:
        super().__init__(NO_KEY)


@dataclass(frozen=True)
class ShareView:
    id: uuid.UUID
    sender: str
    recipient: str
    label: str
    channel_id: str | None
    message_id: str | None
    expires_at: datetime
    agent_id: str | None = None
    key_name: str | None = None


@dataclass(frozen=True)
class Reveal:
    share: ShareView | None
    value: str | None = None
    refusal: RevealRefusal | None = None


def _view(row: SecretShare) -> ShareView:
    return ShareView(
        row.id,
        row.sender_platform_user_id,
        row.recipient_platform_user_id,
        row.label,
        row.channel_id,
        row.message_id,
        row.expires_at,
        row.agent_id,
        row.key_name,
    )


async def clear_lapsed_shares(session: AsyncSession, *, now: datetime, limit: int = 500) -> int:
    """Delete up to `limit` values past their expiry (the scheduler's sweep); rows stay."""
    lapsed = (
        select(SecretShare.id)
        .where(SecretShare.content.is_not(None), SecretShare.expires_at <= now)
        .limit(limit)
    )
    result = await session.execute(
        update(SecretShare).where(SecretShare.id.in_(lapsed)).values(content=None)
    )
    return cast(CursorResult[Any], result).rowcount


async def _lock_sender(session: AsyncSession, tenant_id: uuid.UUID, sender: str) -> None:
    """Serialize one sender's shares, so two at once cannot pass the count together."""
    digest = hashlib.sha256(tenant_id.bytes + b"secret_shares:" + sender.encode()).digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def create_share(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    sender: str,
    recipient: str,
    label: str,
    value: str,
    now: datetime,
    agent_id: str | None = None,
    key_name: str | None = None,
    awaiting_sender: bool = False,
) -> ShareView | None:
    """Store one encrypted share, or None when the sender already has the most live ones.
    `awaiting_sender` holds it until its sender confirms it (`confirm_share`)."""
    cipher = _cipher(session)
    if cipher is None:
        raise SecretShareEncryptionRequiredError()
    await _lock_sender(session, tenant_id, sender)
    live = await session.scalar(
        select(func.count())
        .select_from(SecretShare)
        .where(
            SecretShare.tenant_id == tenant_id,
            SecretShare.platform == platform,
            SecretShare.sender_platform_user_id == sender,
            SecretShare.content.is_not(None),
            SecretShare.expires_at > now,
        )
    )
    if (live or 0) >= MAX_LIVE_PER_SENDER:
        return None
    row = SecretShare(
        tenant_id=tenant_id,
        platform=platform,
        sender_platform_user_id=sender,
        recipient_platform_user_id=recipient,
        label=label,
        content=encrypt_token(cipher, value),
        agent_id=agent_id,
        key_name=key_name,
        awaiting_sender=awaiting_sender,
        expires_at=now + SHARE_LIFETIME,
    )
    session.add(row)
    await session.flush()
    return _view(row)


async def set_share_card(
    session: AsyncSession, share_id: uuid.UUID, *, channel_id: str, message_id: str
) -> None:
    await session.execute(
        update(SecretShare)
        .where(SecretShare.id == share_id)
        .values(channel_id=channel_id, message_id=message_id)
    )


async def delete_share(session: AsyncSession, share_id: uuid.UUID) -> None:
    """Drop a share whose card was never posted, so no value waits with no way to reveal it."""
    await session.execute(delete(SecretShare).where(SecretShare.id == share_id))


def _one(
    tenant_id: uuid.UUID, share_id: uuid.UUID, platform: str
) -> tuple[ColumnElement[bool], ...]:
    return (
        SecretShare.id == share_id,
        SecretShare.tenant_id == tenant_id,
        SecretShare.platform == platform,
    )


async def get_share(
    session: AsyncSession, *, tenant_id: uuid.UUID, share_id: uuid.UUID, platform: str
) -> ShareView | None:
    row = await session.scalar(select(SecretShare).where(*_one(tenant_id, share_id, platform)))
    return None if row is None else _view(row)


async def confirm_share(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    share_id: uuid.UUID,
    platform: str,
    sender: str,
    now: datetime,
) -> Reveal:
    """Release a held share for its card: by its sender only, once, before it expires."""
    row = await session.scalar(
        select(SecretShare).where(*_one(tenant_id, share_id, platform)).with_for_update()
    )
    if row is None:
        return Reveal(None, refusal="gone")
    share = _view(row)
    if row.sender_platform_user_id != sender:
        return Reveal(share, refusal="not_sender")
    if not row.awaiting_sender or row.content is None:
        return Reveal(share, refusal="gone")
    if row.expires_at <= now:
        row.content = None
        await session.flush()
        return Reveal(share, refusal="expired")
    row.awaiting_sender = False
    await session.flush()
    return Reveal(share)


async def reveal_share(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    share_id: uuid.UUID,
    platform: str,
    recipient: str,
    now: datetime,
) -> Reveal:
    """Hand the value to its recipient once: the row lock makes a second click see it gone."""
    row = await session.scalar(
        select(SecretShare).where(*_one(tenant_id, share_id, platform)).with_for_update()
    )
    if row is None or row.awaiting_sender:
        return Reveal(None, refusal="gone")
    share = _view(row)
    if row.recipient_platform_user_id != recipient:
        return Reveal(share, refusal="not_recipient")
    if row.revealed_at is not None:
        return Reveal(share, refusal="revealed")
    if row.content is None or row.expires_at <= now:
        row.content = None
        await session.flush()
        return Reveal(share, refusal="expired")
    cipher = _cipher(session)
    if cipher is None:
        raise SecretShareEncryptionRequiredError()
    value = decrypt_token(cipher, row.content)
    row.content = None
    row.revealed_at = now
    await session.flush()
    return Reveal(share, value=value)


def _of_person(
    tenant_id: uuid.UUID, platform: str, platform_user_id: str
) -> tuple[ColumnElement[bool], ...]:
    return (
        SecretShare.tenant_id == tenant_id,
        SecretShare.platform == platform,
        or_(
            SecretShare.sender_platform_user_id == platform_user_id,
            SecretShare.recipient_platform_user_id == platform_user_id,
        ),
    )


async def count_shares_for_platform_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, platform_user_id: str
) -> int:
    """Shares that name the person as sender or recipient; what the delete below removes."""
    stmt = select(func.count()).select_from(SecretShare)
    return int(
        await session.scalar(stmt.where(*_of_person(tenant_id, platform, platform_user_id))) or 0
    )


async def delete_shares_for_platform_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, platform_user_id: str
) -> int:
    result = await session.execute(
        delete(SecretShare).where(*_of_person(tenant_id, platform, platform_user_id))
    )
    return cast(CursorResult[Any], result).rowcount
