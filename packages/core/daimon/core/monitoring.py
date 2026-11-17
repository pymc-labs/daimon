"""Monitored agents: the one rule every chat and MCP path calls to keep a turn for admins.

`monitor_direct_messages` covers direct messages (Discord, Slack, Teams) and web chat;
`monitor_mcp` covers MCP agent chat and the person's other MCP tool calls on that agent,
by name, time and outcome only. Both are keyed on the agent that answers and read when a
question is asked, so a conversation begun earlier is kept from the switch on, never
before it, and an answer is kept only when its question was.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog
from daimon.core.config import Settings
from daimon.core.features.registry import FEATURES, FeatureSpec
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.snapshot import FeaturePlace, resolve_one
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import monitoring as store
from daimon.core.stores.domain import MonitoredPlatform
from daimon.core.stores.feature_values import list_feature_values
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class MonitoringState:
    """Whether an agent is monitored on a platform, and since when: the deciding row's
    `set_at`, or None when the deployment setting or the default decides."""

    feature: ResolvedFeature
    since: datetime | None

    @property
    def on(self) -> bool:
        return self.feature.on


def monitoring_spec(platform: MonitoredPlatform) -> FeatureSpec:
    return FEATURES["monitor_mcp"] if platform == "mcp" else FEATURES["monitor_direct_messages"]


async def monitoring_state(
    session: AsyncSession,
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: MonitoredPlatform,
) -> MonitoringState:
    """The feature for the agent whose derived id is `agent_id` (MCP knows no other)."""
    spec = monitoring_spec(platform)
    rows = [
        row
        for row in await list_feature_values(session, tenant_id=tenant_id, keys=(spec.key,))
        if row.tier == "workspace"
        or (
            row.tier == "agent"
            and bool(row.agent_id)
            and derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=row.agent_id) == agent_id
        )
    ]
    ma_agent_id = next((row.agent_id for row in rows if row.tier == "agent"), None)
    place = FeaturePlace(tenant_id, platform, agent_id=ma_agent_id)
    feature = resolve_one(spec, settings, place, rows)
    since = next((row.set_at for row in rows if row.tier == feature.tier), None)
    return MonitoringState(feature, since if feature.on else None)


async def capture_question(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: MonitoredPlatform,
    account_id: uuid.UUID | None,
    conversation: str,
    turn_ref: str,
    content: str,
) -> bool:
    """Keep the person's question when the agent is monitored now; True when kept."""
    if account_id is None:
        return False
    try:
        async with sessions() as session, session.begin():
            state = await monitoring_state(
                session, settings, tenant_id=tenant_id, agent_id=agent_id, platform=platform
            )
            if not state.on:
                return False
            subject = store.MonitoredSubject(
                tenant_id, agent_id, platform, account_id, conversation
            )
            entry = store.MonitoredEntry("question", content=content, turn_ref=turn_ref)
            return await store.record_events(session, subject, (entry,)) == 1
    # Keeping a copy for admins never fails the person's turn; the gap is logged.
    except Exception:
        log.warning("monitoring.capture_failed", kind="question", platform=platform, exc_info=True)
        return False


async def capture_answer(
    sessions: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    conversation: str,
    turn_ref: str,
    content: str | None,
    outcome: str,
) -> bool:
    """Keep the agent's answer when its question was kept: a turn is monitored or not as
    a whole, decided when it was asked. True when kept."""
    try:
        async with sessions() as session, session.begin():
            question = await store.get_open_question(
                session, tenant_id=tenant_id, conversation=conversation, turn_ref=turn_ref
            )
            if question is None:
                return False
            subject = store.MonitoredSubject(
                tenant_id, question.agent_id, question.platform, question.account_id, conversation
            )
            entry = store.MonitoredEntry(
                "answer", content=content or None, outcome=outcome, turn_ref=turn_ref
            )
            return await store.record_events(session, subject, (entry,)) == 1
    # As for the question: the person's turn has ended; only the kept copy is missing.
    except Exception:
        log.warning("monitoring.capture_failed", kind="answer", exc_info=True)
        return False


async def open_questions(
    sessions: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, conversation: str
) -> frozenset[str]:
    """The turn refs whose kept question still waits for its answer."""
    async with sessions() as session:
        refs = await store.open_question_refs(
            session, tenant_id=tenant_id, conversation=conversation
        )
    return frozenset(refs)


async def capture_tool_call(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    tool_name: str,
    outcome: str,
    at: datetime | None = None,
) -> bool:
    """Keep an MCP tool call's name, time (`at`, when it began) and outcome, never its
    arguments or result."""
    if account_id is None:
        return False
    try:
        async with sessions() as session, session.begin():
            state = await monitoring_state(
                session, settings, tenant_id=tenant_id, agent_id=agent_id, platform="mcp"
            )
            if not state.on:
                return False
            subject = store.MonitoredSubject(tenant_id, agent_id, "mcp", account_id, None)
            entry = store.MonitoredEntry(
                "tool", tool_name=tool_name, outcome=outcome, created_at=at
            )
            return await store.record_events(session, subject, (entry,)) == 1
    # The tool call already answered; a missing kept copy must not turn it into an error.
    except Exception:
        log.warning("monitoring.capture_failed", kind="tool", exc_info=True)
        return False
