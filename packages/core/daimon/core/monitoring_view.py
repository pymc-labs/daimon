"""Admin reads of monitored agents: the conversations kept for one agent and one
conversation's transcript, shaped once for the web app, chat tools and CLI.

Every read by an admin is written to the audit trail by the caller (`AUDIT_LIST`,
`AUDIT_OPEN`), so admins' viewing shows up in Audit.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, get_args

from daimon.core.config import Settings
from daimon.core.cursor import Cursor
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.monitoring import MonitoringState, monitoring_state
from daimon.core.stores import monitoring as store
from daimon.core.stores.domain import (
    MonitoredConversationRow,
    MonitoredEventRow,
    MonitoredPlatform,
)
from sqlalchemy.ext.asyncio import AsyncSession

AUDIT_LIST = "Viewed monitored activity"
AUDIT_OPEN = "Opened monitored conversation"

#: Where a conversation happened: direct messages, web chat or coding tools (platform `mcp`).
Where = Literal["direct_messages", "web", "coding_tools"]
WHERES: tuple[Where, ...] = get_args(Where)

_PLATFORMS: dict[Where, tuple[MonitoredPlatform, ...]] = {
    "direct_messages": ("discord", "slack", "teams"),
    "web": ("web",),
    "coding_tools": ("mcp",),
}
_TOOLS_PREFIX = "tools:"


def agent_uuid(tenant_id: uuid.UUID, ma_agent_id: str) -> uuid.UUID:
    return derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)


def where_of(platform: MonitoredPlatform) -> Where:
    return next(w for w, platforms in _PLATFORMS.items() if platform in platforms)


def day_range(first: date | None, last: date | None) -> tuple[datetime | None, datetime | None]:
    """UTC bounds of the whole days `first` to `last`, both ends included."""
    since = datetime.combine(first, datetime.min.time(), UTC) if first else None
    until = datetime.combine(last + timedelta(days=1), datetime.min.time(), UTC) if last else None
    return since, until


def conversation_id(row: MonitoredConversationRow) -> str:
    """The row's handle for `get_transcript`: its person and conversation (or tool day)."""
    key = row.conversation if row.conversation is not None else _TOOLS_PREFIX + row.day
    raw = f"{row.account_id}|{key}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_conversation_id(text: str) -> tuple[uuid.UUID, str] | None:
    """The person and conversation key `conversation_id` made, or None for anything else."""
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode()
        account, _, key = raw.partition("|")
        return (uuid.UUID(account), key) if key else None
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


async def list_conversations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    ma_agent_id: str,
    where: Where | None = None,
    account_id: uuid.UUID | None = None,
    first: date | None = None,
    last: date | None = None,
    before: Cursor | None = None,
    limit: int = 100,
) -> list[MonitoredConversationRow]:
    """The agent's kept conversations, most recently active first."""
    since, until = day_range(first, last)
    return await store.list_conversations(
        session,
        tenant_id=tenant_id,
        agent_id=agent_uuid(tenant_id, ma_agent_id),
        since=since,
        until=until,
        platforms=_PLATFORMS[where] if where else None,
        account_id=account_id,
        before=before,
        limit=limit,
    )


async def get_transcript(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    ma_agent_id: str,
    conversation: str,
    after: Cursor | None = None,
    limit: int = 200,
) -> list[MonitoredEventRow] | None:
    """The events of one conversation (`conversation_id`), oldest first from `after`;
    None when the handle is not one."""
    parsed = parse_conversation_id(conversation)
    if parsed is None:
        return None
    account_id, key = parsed
    tools = key.startswith(_TOOLS_PREFIX)
    since = until = None
    if tools:
        try:
            day = date.fromisoformat(key.removeprefix(_TOOLS_PREFIX))
        except ValueError:
            return None
        since, until = day_range(day, day)
    return await store.list_events(
        session,
        tenant_id=tenant_id,
        agent_id=agent_uuid(tenant_id, ma_agent_id),
        account_id=account_id,
        conversation=None if tools else key,
        since=since,
        until=until,
        tool_calls_only=tools,
        after=after,
        oldest_first=True,
        limit=limit,
    )


@dataclass(frozen=True)
class AgentMonitoring:
    """Both switches of one agent, each with when it was turned on."""

    direct_messages: MonitoringState
    coding_tools: MonitoringState


async def agent_monitoring(
    session: AsyncSession, settings: Settings | None, *, tenant_id: uuid.UUID, ma_agent_id: str
) -> AgentMonitoring:
    agent = agent_uuid(tenant_id, ma_agent_id)
    # `web` stands for every direct-message platform: one switch covers them all.
    return AgentMonitoring(
        direct_messages=await monitoring_state(
            session, settings, tenant_id=tenant_id, agent_id=agent, platform="web"
        ),
        coding_tools=await monitoring_state(
            session, settings, tenant_id=tenant_id, agent_id=agent, platform="mcp"
        ),
    )
