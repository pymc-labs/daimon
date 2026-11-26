"""Monitored agents: the one rule every chat and MCP path calls to keep a turn for admins.

`monitor_direct_messages` covers direct messages (Discord, Slack, Teams) and web chat;
`monitor_mcp` covers MCP agent chat and the person's other MCP tool calls on that agent,
by name, time and outcome only, from the call whose result told them. Both are keyed on
the agent that answers and read when a question is asked, so a conversation begun earlier
is kept from the switch on, never before it, and an answer is kept only when its question
was. `must_tell` decides when the person is told, once per person and agent from each
switch on and with the same words on every path, and `record_notice` notes it once shown.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

import structlog
from daimon.core.config import Settings
from daimon.core.features.registry import FEATURES, TIERS, FeatureSpec
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.snapshot import CACHE, FeaturePlace, resolve_one
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import monitoring as store
from daimon.core.stores.domain import FeatureValueRow, MonitoredPlatform
from daimon.core.stores.feature_values import list_feature_values
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

_ANY_TTL = 30.0
_any_memo: dict[tuple[uuid.UUID, MonitoredPlatform], tuple[float, int, bool]] = {}


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
    subject: store.MonitoredSubject | None = None


def must_tell(state: MonitoringState, told_at: datetime | None) -> bool:
    """The one rule: tell the person when the turn is monitored and they were not told
    since monitoring was last switched on. `told_at` is their newest notice row for the
    agent: in any chat, or over MCP (`stores.monitoring.last_notice_at`), so once per
    person and agent, not per conversation."""
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
        if await _any_on(session, settings, tenant_id=tenant_id, platform=where):
            return True
    return False


async def may_monitor(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    platform: MonitoredPlatform,
) -> bool:
    """Whether any agent in the workspace is monitored on `platform`, so a hot path skips
    the rest. Held 30 seconds per process; a feature change in this process drops it."""
    key: tuple[uuid.UUID, MonitoredPlatform] = (tenant_id, platform)
    now, generation = time.monotonic(), CACHE.generation
    hit = _any_memo.get(key)
    if hit is not None and hit[0] > now and hit[1] == generation:
        return hit[2]
    async with sessions() as session:
        on = await _any_on(session, settings, tenant_id=tenant_id, platform=platform)
    if len(_any_memo) >= 4096:
        _any_memo.clear()
    _any_memo[key] = (now + _ANY_TTL, generation, on)
    return on


async def _any_on(
    session: AsyncSession,
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    platform: MonitoredPlatform,
) -> bool:
    spec = monitoring_spec(platform)
    rows = await list_feature_values(session, tenant_id=tenant_id, keys=(spec.key,))
    workspace = [row for row in rows if row.tier == "workspace"]
    if resolve_one(spec, settings, FeaturePlace(tenant_id, platform), workspace).on:
        return True
    for agent in {row.agent_id for row in rows if row.tier == "agent" and row.agent_id}:
        mine = [*workspace, *(r for r in rows if r.tier == "agent" and r.agent_id == agent)]
        place = FeaturePlace(tenant_id, platform, agent_id=agent)
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
    if not feature.on or feature.tier == "default":
        return MonitoringState(feature, None)
    # On since the newest change to a row that decides it now: the deciding row or a
    # broader one, whose lock may just have lifted.
    deciding = TIERS.index(feature.tier)
    since = max((row.set_at for row in rows if TIERS.index(row.tier) <= deciding), default=None)
    return MonitoringState(feature, since)


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
        if not await may_monitor(sessions, settings, tenant_id=tenant_id, platform=platform):
            return None
        async with sessions() as session, session.begin():
            state = await monitoring_state(
                session, settings, tenant_id=tenant_id, agent_id=agent_id, platform=platform
            )
            if not state.on:
                return None
            told_at = await _told_at(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                platform=platform,
                account_id=account_id,
            )
            subject = store.MonitoredSubject(
                tenant_id, agent_id, platform, account_id, conversation
            )
            entry = store.MonitoredEntry("question", content=content, turn_ref=turn_ref)
            kept = await store.record_events(session, subject, (entry,)) == 1
            # A re-run of the same message keeps its first question and the answer it gets.
            if not kept and (
                await store.get_open_question(
                    session, tenant_id=tenant_id, conversation=conversation, turn_ref=turn_ref
                )
                is None
            ):
                return None
            tell = must_tell(state, told_at)
            return KeptQuestion(turn_ref, notice_for(platform) if tell else None, subject)
    # Keeping a copy for admins never fails the person's turn; the gap is logged.
    except Exception:
        log.warning("monitoring.capture_failed", kind="question", platform=platform, exc_info=True)
        return None


async def _told_at(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: MonitoredPlatform,
    account_id: uuid.UUID,
) -> datetime | None:
    """The person's newest notice for this agent, once per person and agent: in any chat,
    or over MCP."""
    return await store.last_notice_at(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=account_id,
        mcp=platform == "mcp",
    )


async def record_notice(
    sessions: async_sessionmaker[AsyncSession],
    subject: store.MonitoredSubject | None,
    notice: MonitoringNotice,
    *,
    turn_ref: str | None = None,
) -> bool:
    """Note that `subject`'s person was shown `notice`, once it reached them; `must_tell`
    reads the newest. True when kept."""
    if subject is None:
        return False
    try:
        async with sessions() as session, session.begin():
            entry = store.MonitoredEntry("notice", content=notice.text, turn_ref=turn_ref)
            return await store.record_events(session, subject, (entry,)) == 1
    # Unnoted, the person is simply told again next time; it never fails their turn.
    except Exception:
        log.warning(
            "monitoring.capture_failed", kind="notice", platform=subject.platform, exc_info=True
        )
        return False


async def monitored_notice(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: MonitoredPlatform,
    account_id: uuid.UUID | None = None,
) -> MonitoringNotice | None:
    """The notice a place shown before any question carries while the agent is
    monitored (web chat, `describe_agent`, the MCP briefing); None when it is not. With
    `account_id`, only until that person was told or dismissed it (`must_tell`)."""
    if not await may_monitor(sessions, settings, tenant_id=tenant_id, platform=platform):
        return None
    async with sessions() as session:
        state = await monitoring_state(
            session, settings, tenant_id=tenant_id, agent_id=agent_id, platform=platform
        )
        if account_id is not None:
            told_at = await _told_at(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                platform=platform,
                account_id=account_id,
            )
            return notice_for(platform) if must_tell(state, told_at) else None
    return notice_for(platform) if state.on else None


async def dismiss_notice(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    conversation: str,
) -> bool:
    """Note that a web chat person dismissed the notice above the composer, which counts as
    told until monitoring is switched on again. True when noted; a repeat is a no-op."""
    async with sessions() as session:
        state = await monitoring_state(
            session, settings, tenant_id=tenant_id, agent_id=agent_id, platform="web"
        )
        told_at = await _told_at(
            session, tenant_id=tenant_id, agent_id=agent_id, platform="web", account_id=account_id
        )
    if not must_tell(state, told_at):
        return False
    subject = store.MonitoredSubject(tenant_id, agent_id, "web", account_id, conversation)
    return await record_notice(sessions, subject, CHAT_NOTICE)


async def capture_answer(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
    *,
    tenant_id: uuid.UUID,
    conversation: str,
    turn_ref: str,
    content: str | None,
    outcome: str,
    question_ref: str | None = None,
) -> bool:
    """Keep the agent's answer when its question was kept: a turn is monitored or not as
    a whole, decided when it was asked. A continuation passes the `question_ref` it carries
    on and its own `turn_ref`; it is kept only while the question's agent is still
    monitored, since the question's own turn has long ended by then. True when kept."""
    try:
        async with sessions() as session, session.begin():
            if question_ref is None:
                question = await store.get_open_question(
                    session, tenant_id=tenant_id, conversation=conversation, turn_ref=turn_ref
                )
            else:
                question = await store.get_question(
                    session, tenant_id=tenant_id, conversation=conversation, turn_ref=question_ref
                )
            if question is None:
                return False
            if question_ref is not None:
                state = await monitoring_state(
                    session,
                    settings,
                    tenant_id=tenant_id,
                    agent_id=question.agent_id,
                    platform=question.platform,
                )
                if not state.on:
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


async def continued_question(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: str,
    conversation: str,
    account_id: uuid.UUID,
    queued_at: datetime,
) -> str | None:
    """The ref of the kept question a continuation carries on: the person's question to
    `agent_id` in `conversation` that was still waiting for its answer when the work was
    queued. None unless the agent is monitored now, and a question kept before monitoring
    was last switched on is no longer carried on."""
    if platform not in ("discord", "slack", "teams", "web"):
        return None
    try:
        if not await may_monitor(sessions, settings, tenant_id=tenant_id, platform=platform):
            return None
        async with sessions() as session:
            state = await monitoring_state(
                session, settings, tenant_id=tenant_id, agent_id=agent_id, platform=platform
            )
            if not state.on:
                return None
            return await store.question_open_at(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                conversation=conversation,
                account_id=account_id,
                at=queued_at,
                since=state.since,
            )
    # As for the question: the queued work runs on; only the kept copy is missing.
    except Exception:
        log.warning("monitoring.capture_failed", kind="continuation", exc_info=True)
        return None


async def open_questions(
    sessions: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, conversation: str
) -> frozenset[str]:
    """The turn refs whose kept question still waits for its answer."""
    async with sessions() as session:
        refs = await store.open_question_refs(
            session, tenant_id=tenant_id, conversation=conversation
        )
    return frozenset(refs)


async def mcp_notice_due(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
) -> bool:
    """Whether a person's MCP tool call on the agent must carry the notice: monitored, and
    they were not told since it was switched on. Until then their calls are not kept."""
    try:
        async with sessions() as session:
            untold = await _untold(
                session, settings, tenant_id=tenant_id, agent_id=agent_id, account_id=account_id
            )
        return untold is True
    # Unknown, the result goes back as it is; its call is kept only once they are told.
    except Exception:
        log.warning("monitoring.capture_failed", kind="notice", platform="mcp", exc_info=True)
        return False


async def _untold(
    session: AsyncSession,
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
) -> bool | None:
    """None when the agent is not monitored over MCP, else whether the person must be told."""
    state = await monitoring_state(
        session, settings, tenant_id=tenant_id, agent_id=agent_id, platform="mcp"
    )
    if not state.on:
        return None
    told_at = await _told_at(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        platform="mcp",
        account_id=account_id,
    )
    return must_tell(state, told_at)


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
    arguments or result, once the person was told (`mcp_notice_due`)."""
    if account_id is None:
        return False
    try:
        async with sessions() as session, session.begin():
            untold = await _untold(
                session, settings, tenant_id=tenant_id, agent_id=agent_id, account_id=account_id
            )
            if untold is not False:
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
