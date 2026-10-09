"""Claim and settle private follow-up notices for self-serve GitHub connections."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Literal

from daimon.core._models import GitHubConnectInvitation
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession


class ConnectNotice(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    token_hash: str
    tenant_id: uuid.UUID
    requester_platform_user_id: str
    agent_name: str | None
    encrypted_origin_followup: bytes | None = None
    origin_followup_expires_at: datetime | None = None
    connected_repos: list[dict[str, str]]
    notice_claimed_at: datetime

    @property
    def text(self) -> str:
        names = ", ".join(repo["name"] for repo in self.connected_repos)
        access = {repo["access"] for repo in self.connected_repos}
        level = "Read and write" if access == {"write"} else "Read only"
        return f"Connected {names}, {level}. Ready."


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
    else:
        row.notice_claimed_at = None
