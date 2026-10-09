"""Claim and settle GitHub connect confirmations at their originating conversation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Literal

from daimon.core._models import GitHubConnectInvitation
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession


class ConnectNotice(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    token_hash: str
    tenant_id: uuid.UUID
    requester_platform_user_id: str
    agent_name: str | None
    origin_parent_channel_id: str | None = None
    origin_thread_id: str | None = None
    encrypted_origin_followup: bytes | None = None
    origin_followup_expires_at: datetime | None = None
    connected_repos: list[dict[str, str]]
    notice_claimed_at: datetime
    notice_attempts: int = 0

    @property
    def text(self) -> str:
        names = ", ".join(repo["name"] for repo in self.connected_repos)
        return f"Connected {names}, {self.level}. Ready."

    @property
    def public_text(self) -> str:
        return f"Connected {len(self.connected_repos)} repo(s), {self.level}. Ready."

    @property
    def level(self) -> str:
        access = {repo["access"] for repo in self.connected_repos}
        if access == {"write"}:
            return "Read and write"
        if access == {"read"}:
            return "Read only"
        return "Mixed access"


async def expire_old(
    session: AsyncSession, *, platform: Literal["discord", "slack"], now: datetime
) -> None:
    """Stop abandoned confirmations and erase spent interaction credentials."""
    await session.execute(
        update(GitHubConnectInvitation)
        .where(
            GitHubConnectInvitation.origin_platform == platform,
            GitHubConnectInvitation.used_at.is_not(None),
            GitHubConnectInvitation.notice_delivered_at.is_(None),
            GitHubConnectInvitation.connected_repos.is_not(None),
            or_(
                GitHubConnectInvitation.used_at <= now - timedelta(hours=24),
                GitHubConnectInvitation.notice_attempts >= 8,
            ),
        )
        .values(notice_delivered_at=now, encrypted_origin_followup=None)
    )
    await session.execute(
        update(GitHubConnectInvitation)
        .where(
            GitHubConnectInvitation.origin_platform == platform,
            GitHubConnectInvitation.encrypted_origin_followup.is_not(None),
            GitHubConnectInvitation.origin_followup_expires_at <= now,
        )
        .values(encrypted_origin_followup=None)
    )


async def claim_next(
    session: AsyncSession, *, platform: Literal["discord", "slack"], now: datetime
) -> ConnectNotice | None:
    row = await session.scalar(
        select(GitHubConnectInvitation)
        .where(
            GitHubConnectInvitation.origin_platform == platform,
            GitHubConnectInvitation.requester_platform_user_id.is_not(None),
            GitHubConnectInvitation.operator_issued.is_(False),
            GitHubConnectInvitation.used_at.is_not(None),
            GitHubConnectInvitation.activation_status.is_distinct_from("update_pending"),
            GitHubConnectInvitation.connected_repos.is_not(None),
            GitHubConnectInvitation.notice_delivered_at.is_(None),
            GitHubConnectInvitation.requested_work.is_(None),
            GitHubConnectInvitation.notice_attempts < 8,
            GitHubConnectInvitation.used_at > now - timedelta(hours=24),
            or_(
                GitHubConnectInvitation.notice_next_attempt_at.is_(None),
                GitHubConnectInvitation.notice_next_attempt_at <= now,
            ),
            or_(
                GitHubConnectInvitation.notice_claimed_at.is_(None),
                GitHubConnectInvitation.notice_claimed_at < now - timedelta(minutes=10),
            ),
        )
        .order_by(GitHubConnectInvitation.used_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    row.notice_claimed_at = now
    row.notice_attempts += 1
    await session.flush()
    return ConnectNotice.model_validate(row)


async def settle(
    session: AsyncSession, *, notice: ConnectNotice, delivered: bool, now: datetime
) -> None:
    row = await session.get(GitHubConnectInvitation, notice.token_hash, with_for_update=True)
    if row is None or row.notice_claimed_at != notice.notice_claimed_at:
        return
    if delivered:
        row.notice_delivered_at = now
        row.encrypted_origin_followup = None
    else:
        row.notice_claimed_at = None
        row.notice_next_attempt_at = now + timedelta(
            minutes=min(2 ** min(row.notice_attempts, 6), 60)
        )
        if row.notice_attempts >= 8 or (
            row.used_at is not None and row.used_at <= now - timedelta(hours=24)
        ):
            row.notice_delivered_at = now
            row.encrypted_origin_followup = None
