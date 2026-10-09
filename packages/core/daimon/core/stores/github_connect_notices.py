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
    origin_parent_channel_id: str | None
    origin_thread_id: str | None
    connected_repos: list[dict[str, str]]
    notice_claimed_at: datetime

    @property
    def access_label(self) -> str:
        access = {repo["access"] for repo in self.connected_repos}
        return "Read and write" if access == {"write"} else "Read only"

    @property
    def in_thread(self) -> bool:
        channel = self.origin_parent_channel_id
        return (
            channel is not None
            and self.origin_thread_id is not None
            and not channel.startswith("D")
        )

    @property
    def text(self) -> str:
        if self.in_thread:
            return (
                f"GitHub connected: {len(self.connected_repos)} repo(s), "
                f"{self.access_label}. What should I do first?"
            )
        names = ", ".join(repo["name"] for repo in self.connected_repos)
        target = self.agent_name or "your workspace"
        mention = self.agent_name or "an agent"
        return (
            f"GitHub connected for {target}: {names}, {self.access_label}. "
            f"Mention {mention} in a channel to start."
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
