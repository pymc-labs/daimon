"""Durable, asker-bound GitHub access decisions for unfinished thread work.

This store records intent and decisions. Dispatching a continued turn belongs
to the platform continuation dispatcher after it can preserve finished work.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from daimon.core._models import (
    Account,
    GitHubAccessRequest,
    GitHubAccessRequestDelivery,
    PlatformPrincipal,
)
from daimon.core.stores.task_continuations import record_continuation
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

RequestStatus = Literal["open", "waiting_github", "ready", "cancelled", "declined", "expired"]
_ACTIVE = ("open", "waiting_github")
_REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_LIFETIME = timedelta(days=7)


class AccessRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    requester_account_id: uuid.UUID
    requester_platform_user_id: str
    platform: str
    parent_channel_id: str
    thread_id: str
    agent_id: uuid.UUID
    ma_agent_id: str
    agent_name: str
    repo_names: list[str]
    required_ability: Literal["read", "write"]
    approved_by_account_id: uuid.UUID | None
    requested_work: str | None
    status: RequestStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    admin_notified_at: datetime | None
    admin_card_message_id: str | None = None
    resumed_at: datetime | None
    expiry_notice_sent_at: datetime | None = None


class RequestDelivery(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    request_id: uuid.UUID
    recipient_account_id: uuid.UUID
    platform_user_id: str
    message_id: str | None
    delivered_at: datetime | None
    dismissed_at: datetime | None


class AdminRecipient(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_id: uuid.UUID
    platform_user_id: str


async def list_server_admin_recipients(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: Literal["discord", "slack"],
    exclude_account_id: uuid.UUID | None = None,
    limit: int = 3,
) -> list[AdminRecipient]:
    rows = await session.execute(
        select(Account.id, PlatformPrincipal.external_id)
        .join(PlatformPrincipal, PlatformPrincipal.account_id == Account.id)
        .where(
            Account.tenant_id == tenant_id,
            Account.role == "admin",
            Account.is_external.is_(False),
            PlatformPrincipal.tenant_id == tenant_id,
            PlatformPrincipal.platform == platform,
            *([Account.id != exclude_account_id] if exclude_account_id is not None else []),
        )
        .order_by(Account.id)
        .limit(limit)
    )
    return [
        AdminRecipient(account_id=account_id, platform_user_id=platform_user_id)
        for account_id, platform_user_id in rows
    ]


async def request_access(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    requester_account_id: uuid.UUID,
    requester_platform_user_id: str,
    platform: Literal["discord", "slack"],
    parent_channel_id: str,
    thread_id: str,
    agent_id: uuid.UUID,
    ma_agent_id: str,
    agent_name: str,
    repo_name: str,
    requested_work: str | None,
    is_admin: bool,
    required_ability: Literal["read", "write"] = "read",
    now: datetime | None = None,
) -> AccessRequest:
    """Keep each approval card limited to one repo and its own ability."""
    current = now or datetime.now(UTC)
    if not _REPO_NAME.fullmatch(repo_name) or len(repo_name) > 255:
        raise ValueError("Name one GitHub repo as owner/repo.")
    if not thread_id or not parent_channel_id or not requester_platform_user_id:
        raise ValueError("This request needs a conversation and an asker.")
    account = await session.get(Account, requester_account_id)
    if account is None or account.tenant_id != tenant_id or account.is_external:
        raise ValueError("GitHub requests are unavailable here.")
    key = f"github-access:{tenant_id}:{platform}:{thread_id}:{requester_account_id}:{agent_id}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
    )
    row = await session.scalar(
        select(GitHubAccessRequest)
        .where(
            GitHubAccessRequest.tenant_id == tenant_id,
            GitHubAccessRequest.platform == platform,
            GitHubAccessRequest.thread_id == thread_id,
            GitHubAccessRequest.requester_account_id == requester_account_id,
            GitHubAccessRequest.agent_id == agent_id,
            GitHubAccessRequest.status.in_(_ACTIVE),
        )
        .with_for_update()
    )
    if row is not None and row.expires_at <= current:
        row.status = "expired"
        row.updated_at = current
        await session.flush()
        row = None
    if row is not None and (
        repo_name.casefold() not in {name.casefold() for name in row.repo_names}
        or row.required_ability != required_ability
    ):
        # Only one live card is allowed per thread. Supersede the old decision
        # so the new card cannot inherit its write access or approve its repo.
        row.status = "cancelled"
        row.updated_at = current
        await session.flush()
        row = None
    if row is None:
        if not is_admin:
            since = current - timedelta(hours=24)
            count = await session.scalar(
                select(func.count(GitHubAccessRequest.id)).where(
                    GitHubAccessRequest.tenant_id == tenant_id,
                    GitHubAccessRequest.requester_account_id == requester_account_id,
                    GitHubAccessRequest.created_at >= since,
                )
            )
            if (count or 0) >= 3:
                raise ValueError("Your admins already have requests from you waiting.")
        row = GitHubAccessRequest(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            requester_account_id=requester_account_id,
            requester_platform_user_id=requester_platform_user_id,
            platform=platform,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            agent_id=agent_id,
            ma_agent_id=ma_agent_id,
            agent_name=agent_name,
            repo_names=[],
            required_ability=required_ability,
            created_at=current,
            updated_at=current,
            expires_at=current + _LIFETIME,
        )
        session.add(row)
    row.repo_names = [repo_name]
    row.requested_work = requested_work[:500] if requested_work else None
    row.required_ability = required_ability
    row.agent_name = agent_name
    row.ma_agent_id = ma_agent_id
    row.updated_at = current
    await session.flush()
    return AccessRequest.model_validate(row)


async def get_request(
    session: AsyncSession, *, tenant_id: uuid.UUID, request_id: uuid.UUID
) -> AccessRequest | None:
    row = await session.get(GitHubAccessRequest, request_id)
    return (
        AccessRequest.model_validate(row)
        if row is not None and row.tenant_id == tenant_id
        else None
    )


async def lookup_request(session: AsyncSession, *, request_id: uuid.UUID) -> AccessRequest | None:
    """Find a card's tenant; callers must verify its recipient before disclosing it."""
    row = await session.get(GitHubAccessRequest, request_id)
    return AccessRequest.model_validate(row) if row is not None else None


async def list_asker_requests(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    now: datetime | None = None,
) -> list[AccessRequest]:
    current = now or datetime.now(UTC)
    rows = await session.scalars(
        select(GitHubAccessRequest)
        .where(
            GitHubAccessRequest.tenant_id == tenant_id,
            GitHubAccessRequest.requester_account_id == account_id,
            GitHubAccessRequest.status.in_(_ACTIVE),
            GitHubAccessRequest.expires_at > current,
        )
        .order_by(GitHubAccessRequest.created_at.desc())
    )
    return [AccessRequest.model_validate(row) for row in rows]


async def list_waiting(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    visible_agent_ids: frozenset[uuid.UUID],
    now: datetime | None = None,
) -> list[AccessRequest]:
    """List only requests for agents the caller has independently proved visible."""
    if not visible_agent_ids:
        return []
    current = now or datetime.now(UTC)
    rows = await session.scalars(
        select(GitHubAccessRequest)
        .where(
            GitHubAccessRequest.tenant_id == tenant_id,
            GitHubAccessRequest.agent_id.in_(visible_agent_ids),
            GitHubAccessRequest.status.in_(_ACTIVE),
            or_(
                GitHubAccessRequest.status == "waiting_github",
                GitHubAccessRequest.approved_by_account_id.is_(None),
            ),
            GitHubAccessRequest.expires_at > current,
        )
        .order_by(GitHubAccessRequest.created_at)
    )
    return [AccessRequest.model_validate(row) for row in rows]


async def cancel_request(
    session: AsyncSession, *, tenant_id: uuid.UUID, request_id: uuid.UUID, account_id: uuid.UUID
) -> bool:
    row = await session.get(GitHubAccessRequest, request_id, with_for_update=True)
    if (
        row is None
        or row.tenant_id != tenant_id
        or row.requester_account_id != account_id
        or row.status not in (*_ACTIVE, "declined")
    ):
        return False
    row.status = "cancelled"
    row.updated_at = datetime.now(UTC)
    return True


async def set_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    expected: RequestStatus,
    status: RequestStatus,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(UTC)
    row = await session.get(GitHubAccessRequest, request_id, with_for_update=True)
    if row is None or row.tenant_id != tenant_id or row.status != expected:
        return False
    if row.expires_at <= current and status not in ("expired", "cancelled"):
        row.status = "expired"
        row.updated_at = current
        return False
    row.status = status
    row.updated_at = current
    return True


async def ready_and_continue(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    now: datetime | None = None,
) -> bool:
    """Close one pending request and enqueue its remaining work atomically."""
    current = now or datetime.now(UTC)
    row = await session.get(GitHubAccessRequest, request_id, with_for_update=True)
    if row is None or row.tenant_id != tenant_id or row.status not in _ACTIVE:
        return False
    if row.expires_at <= current:
        row.status = "expired"
        row.updated_at = current
        return False
    row.status = "ready"
    row.updated_at = current
    if row.requested_work:
        await record_continuation(
            session,
            tenant_id=row.tenant_id,
            platform=row.platform,
            parent_channel_id=row.parent_channel_id,
            thread_id=row.thread_id,
            requester_account_id=row.requester_account_id,
            requester_external_user_id=row.requester_platform_user_id,
            target_ma_agent_id=row.ma_agent_id,
            target_name=row.agent_name,
            reason="github_access_ready",
            idempotency_key=row.id,
            requested_work=row.requested_work,
            available_at=current,
        )
    return True


async def claim_due_expiry_group(
    session: AsyncSession,
    *,
    platform: Literal["discord", "slack"],
    now: datetime,
) -> list[AccessRequest]:
    """Lock expired requests in one thread for one final status line."""
    due = await session.execute(
        select(
            GitHubAccessRequest.tenant_id,
            GitHubAccessRequest.thread_id,
        )
        .where(
            GitHubAccessRequest.platform == platform,
            GitHubAccessRequest.expires_at <= now,
            GitHubAccessRequest.expiry_notice_sent_at.is_(None),
            GitHubAccessRequest.status.in_((*_ACTIVE, "expired")),
        )
        .order_by(GitHubAccessRequest.expires_at, GitHubAccessRequest.id)
        .limit(1)
    )
    candidate = due.first()
    if candidate is None:
        return []
    tenant_id, thread_id = candidate
    key = f"github-expiry:{platform}:{tenant_id}:{thread_id}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
    )
    rows = await session.scalars(
        select(GitHubAccessRequest)
        .where(
            GitHubAccessRequest.tenant_id == tenant_id,
            GitHubAccessRequest.platform == platform,
            GitHubAccessRequest.thread_id == thread_id,
            GitHubAccessRequest.expires_at <= now,
            GitHubAccessRequest.expiry_notice_sent_at.is_(None),
            GitHubAccessRequest.status.in_((*_ACTIVE, "expired")),
        )
        .order_by(GitHubAccessRequest.expires_at, GitHubAccessRequest.id)
        .with_for_update()
    )
    group = list(rows)
    for row in group:
        row.status = "expired"
        row.updated_at = now
    await session.flush()
    return [AccessRequest.model_validate(row) for row in group]


async def mark_expiry_notice_sent(
    session: AsyncSession, *, request_ids: tuple[uuid.UUID, ...], now: datetime
) -> None:
    for request_id in request_ids:
        row = await session.get(GitHubAccessRequest, request_id)
        if row is not None and row.status == "expired":
            row.expiry_notice_sent_at = now


async def get_delivery(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    recipient_account_id: uuid.UUID,
) -> RequestDelivery | None:
    request = await get_request(session, tenant_id=tenant_id, request_id=request_id)
    if request is None:
        return None
    row = await session.get(GitHubAccessRequestDelivery, (request_id, recipient_account_id))
    return RequestDelivery.model_validate(row) if row is not None else None


async def lock_delivery_slot(
    session: AsyncSession, *, request_id: uuid.UUID, recipient_account_id: uuid.UUID
) -> None:
    """Serialize a post/edit across MCP turns so a person gets one card."""
    key = f"github-request-card:{request_id}:{recipient_account_id}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
    )


async def lock_shared_card_slot(session: AsyncSession, *, request: AccessRequest) -> None:
    """Serialize posts and the per-thread mention budget across requests."""
    key = f"github-request-admin-card:{request.tenant_id}:{request.platform}:{request.thread_id}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
    )


async def shared_card_mention_allowed(session: AsyncSession, *, request: AccessRequest) -> bool:
    since = datetime.now(UTC) - timedelta(hours=1)
    count = await session.scalar(
        select(func.count(GitHubAccessRequest.id)).where(
            GitHubAccessRequest.tenant_id == request.tenant_id,
            GitHubAccessRequest.platform == request.platform,
            GitHubAccessRequest.thread_id == request.thread_id,
            GitHubAccessRequest.admin_notified_at >= since,
        )
    )
    return (count or 0) < 3


async def record_shared_card(
    session: AsyncSession, *, request: AccessRequest, message_id: str, mentioned: bool
) -> None:
    row = await session.get(GitHubAccessRequest, request.id, with_for_update=True)
    if row is None or row.tenant_id != request.tenant_id:
        return
    row.admin_card_message_id = message_id
    if mentioned:
        row.admin_notified_at = datetime.now(UTC)


async def record_delivery(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    recipient_account_id: uuid.UUID,
    platform_user_id: str,
    message_id: str,
) -> bool:
    request = await session.get(GitHubAccessRequest, request_id, with_for_update=True)
    recipient = await session.get(Account, recipient_account_id)
    if (
        request is None
        or request.tenant_id != tenant_id
        or request.status not in _ACTIVE
        or request.expires_at <= datetime.now(UTC)
        or recipient is None
        or recipient.tenant_id != tenant_id
        or recipient.is_external
    ):
        return False
    row = await session.get(GitHubAccessRequestDelivery, (request_id, recipient_account_id))
    if row is None:
        row = GitHubAccessRequestDelivery(
            request_id=request_id,
            recipient_account_id=recipient_account_id,
            platform_user_id=platform_user_id,
        )
        session.add(row)
    elif row.dismissed_at is not None or (
        request.platform != "slack" and row.message_id not in (None, message_id)
    ):
        return False
    row.message_id = message_id
    row.delivered_at = datetime.now(UTC)
    if recipient_account_id != request.requester_account_id:
        request.admin_notified_at = row.delivered_at
    return True


async def record_reposted_requester_card(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    recipient_account_id: uuid.UUID,
    message_id: str,
) -> bool:
    """Bind a new Slack ephemeral card even after an admin has declined."""
    request = await session.get(GitHubAccessRequest, request_id)
    row = await session.get(
        GitHubAccessRequestDelivery, (request_id, recipient_account_id), with_for_update=True
    )
    if (
        request is None
        or request.tenant_id != tenant_id
        or request.platform != "slack"
        or request.requester_account_id != recipient_account_id
        or row is None
        or row.dismissed_at is not None
    ):
        return False
    row.message_id = message_id
    row.delivered_at = datetime.now(UTC)
    return True


async def dismiss_delivery(
    session: AsyncSession, *, tenant_id: uuid.UUID, request_id: uuid.UUID, account_id: uuid.UUID
) -> bool:
    request = await session.get(GitHubAccessRequest, request_id)
    if (
        request is None
        or request.tenant_id != tenant_id
        or request.status not in _ACTIVE
        or request.requester_account_id == account_id
    ):
        return False
    row = await session.get(
        GitHubAccessRequestDelivery, (request_id, account_id), with_for_update=True
    )
    if row is None or row.dismissed_at is not None:
        return False
    row.dismissed_at = datetime.now(UTC)
    return True
