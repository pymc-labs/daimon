"""Shared secrets: encrypted on write, revealed once to their recipient, then cleared.

Callers own transactions. A value is never logged, returned by a listing or kept after
its reveal or expiry; there is no plaintext fallback (`NO_KEY` without deployment keys).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from daimon.core._models import SecretShare
from daimon.core.errors import StoreError
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.secret_shares import MAX_LIVE_PER_SENDER, NO_KEY, SHARE_LIFETIME, RevealRefusal
from daimon.core.stores.agent_files import _cipher  # pyright: ignore[reportPrivateUsage]
from sqlalchemy import ColumnElement, delete, func, or_, select, update
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
    )


async def _clear_lapsed(session: AsyncSession, now: datetime) -> None:
    await session.execute(
        update(SecretShare)
        .where(SecretShare.content.is_not(None), SecretShare.expires_at <= now)
        .values(content=None)
    )


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
) -> ShareView | None:
    """Store one encrypted share, or None when the sender already has the most live ones."""
    cipher = _cipher(session)
    if cipher is None:
        raise SecretShareEncryptionRequiredError()
    await _clear_lapsed(session, now)
    live = await session.scalar(
        select(func.count())
        .select_from(SecretShare)
        .where(
            SecretShare.tenant_id == tenant_id,
            SecretShare.platform == platform,
            SecretShare.sender_platform_user_id == sender,
            SecretShare.content.is_not(None),
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


async def reveal_share(
    session: AsyncSession, *, share_id: uuid.UUID, platform: str, recipient: str, now: datetime
) -> Reveal:
    """Hand the value to its recipient once: the row lock makes a second click see it gone."""
    row = await session.scalar(
        select(SecretShare)
        .where(SecretShare.id == share_id, SecretShare.platform == platform)
        .with_for_update()
    )
    if row is None:
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
