"""Monitored agents: the one rule every chat and MCP path calls to keep a turn for admins.

`monitor_direct_messages` covers direct messages (Discord, Slack, Teams) and web chat;
`monitor_mcp` covers MCP agent chat and the person's other MCP tool calls on that agent,
by name, time and outcome only. Both are keyed on the agent that answers and read when a
question is asked, so a conversation begun earlier is kept from the switch on, never
before it, and an answer is kept only when its question was. `must_tell` decides when the
person is told, with the same words on every path.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

import structlog
from daimon.core.config import Settings
from daimon.core.features.registry import FEATURES, FeatureSpec
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.snapshot import FeaturePlace, resolve_one
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import monitoring as store
from daimon.core.stores.domain import FeatureValueRow, MonitoredPlatform
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


@dataclass(frozen=True)
class MonitoringNotice:
    """What a monitored person is told: what is happening, then who can help."""

    line: str
    help: str

    @property
    def text(self) -> str:
        return f"{self.line}\n\n{self.help}"


_HELP = "Ask an admin if you have questions."
CHAT_NOTICE = MonitoringNotice("Admins can read what you and this agent say here.", _HELP)
MCP_NOTICE = MonitoringNotice(
    "Admins can read what you and this agent say, and see which of its tools you use.", _HELP
)


def notice_for(platform: MonitoredPlatform) -> MonitoringNotice:
    return MCP_NOTICE if platform == "mcp" else CHAT_NOTICE


@dataclass(frozen=True)
class KeptQuestion:
    """A kept question; `notice` is what the person must be told now, if anything."""

    turn_ref: str
    notice: MonitoringNotice | None


def must_tell(state: MonitoringState, told_at: datetime | None) -> bool:
    """The one rule: tell the person when the turn is monitored and this conversation
    was not told since monitoring was last switched on. `told_at` is the conversation's
    newest kept question before this one: the first kept question after a switch tells."""
    if not state.on:
        return False
    return told_at is None or (state.since is not None and told_at < state.since)


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
    rows = await list_feature_values(session, tenant_id=tenant_id, keys=(spec.key,))
    return _state(spec, settings, rows, tenant_id=tenant_id, agent_id=agent_id, platform=platform)


async def monitored_agents(
    session: AsyncSession,
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    agent_ids: Iterable[uuid.UUID],
    platform: MonitoredPlatform,
) -> set[uuid.UUID]:
    """Which of `agent_ids` are monitored, from one read of the workspace's settings."""
    spec = monitoring_spec(platform)
    rows = await list_feature_values(session, tenant_id=tenant_id, keys=(spec.key,))
    return {
        agent_id
        for agent_id in agent_ids
        if _state(
            spec, settings, rows, tenant_id=tenant_id, agent_id=agent_id, platform=platform
        ).on
    }


async def monitors_any(
    session: AsyncSession,
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    platform: MonitoredPlatform,
) -> bool:
    """Whether admins monitor any agent in the workspace, here or over MCP (the privacy
    panel's line)."""
    for where in dict.fromkeys((platform, "mcp")):
        spec = monitoring_spec(where)
        rows = await list_feature_values(session, tenant_id=tenant_id, keys=(spec.key,))
        workspace = [row for row in rows if row.tier == "workspace"]
        if resolve_one(spec, settings, FeaturePlace(tenant_id, where), workspace).on:
            return True
        for agent in {row.agent_id for row in rows if row.tier == "agent" and row.agent_id}:
            mine = [*workspace, *(r for r in rows if r.tier == "agent" and r.agent_id == agent)]
            place = FeaturePlace(tenant_id, where, agent_id=agent)
            if resolve_one(spec, settings, place, mine).on:
                return True
    return False


def _state(
    spec: FeatureSpec,
    settings: Settings | None,
    all_rows: Sequence[FeatureValueRow],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: MonitoredPlatform,
) -> MonitoringState:
    rows = [
        row
        for row in all_rows
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
) -> KeptQuestion | None:
    """Keep the person's question when the agent is monitored now, saying whether they
    must be told (`must_tell`); None when nothing was kept."""
    if account_id is None:
        return None
    try:
        async with sessions() as session, session.begin():
            state = await monitoring_state(
                session, settings, tenant_id=tenant_id, agent_id=agent_id, platform=platform
            )
            if not state.on:
                return None
            told_at = await store.last_question_at(
                session, tenant_id=tenant_id, agent_id=agent_id, conversation=conversation
            )
            subject = store.MonitoredSubject(
                tenant_id, agent_id, platform, account_id, conversation
            )
            entry = store.MonitoredEntry("question", content=content, turn_ref=turn_ref)
            if await store.record_events(session, subject, (entry,)) != 1:
                return None
            tell = must_tell(state, told_at)
            return KeptQuestion(turn_ref, notice_for(platform) if tell else None)
    # Keeping a copy for admins never fails the person's turn; the gap is logged.
    except Exception:
        log.warning("monitoring.capture_failed", kind="question", platform=platform, exc_info=True)
        return None


async def monitored_notice(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: MonitoredPlatform,
) -> MonitoringNotice | None:
    """The notice a place shown before any question carries while the agent is
    monitored (web chat, `describe_agent`, the MCP briefing); None when it is not."""
    async with sessions() as session:
        state = await monitoring_state(
            session, settings, tenant_id=tenant_id, agent_id=agent_id, platform=platform
        )
    return notice_for(platform) if state.on else None


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
