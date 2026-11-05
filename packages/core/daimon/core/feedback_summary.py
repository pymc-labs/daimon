"""What people said about answers, counted by agent: thumbs and the reasons picked.

Counts only. The text a person wrote, the message and who voted never leave the store
through here, so an admin reads how an agent is doing, not who said what.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from daimon.core.authz import Subject
from daimon.core.errors import UserFacingError
from daimon.core.message_feedback import FEEDBACK_REASONS
from daimon.core.stores import message_feedback as store
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "MIN_SHOWN",
    "UNKNOWN_AGENT",
    "AgentFeedback",
    "AgentFeedbackOut",
    "FeedbackNotAllowed",
    "FeedbackOut",
    "ReasonCount",
    "feedback_report",
    "feedback_summary",
    "shown",
]

UNKNOWN_AGENT = "Unknown agent"
MIN_SHOWN = 3


class FeedbackNotAllowed(UserFacingError):
    """Only a workspace admin reads feedback."""


@dataclass(slots=True)
class AgentFeedback:
    """One agent's votes in a period; `ma_agent_id` is None where the agent is unknown."""

    ma_agent_id: str | None
    up: int = 0
    down: int = 0
    reasons: dict[str, int] = field(default_factory=dict[str, int])


async def feedback_summary(
    session: AsyncSession,
    *,
    subject: Subject,
    tenant_id: uuid.UUID,
    since: datetime,
    until: datetime,
) -> list[AgentFeedback]:
    """Votes and reasons per agent cast in `[since, until)`, the most voted first."""
    if not subject.is_admin or subject.via_agent_key or subject.client:
        raise FeedbackNotAllowed("Ask an admin or an owner.")
    found: dict[str | None, AgentFeedback] = {}
    for agent_id, vote, count in await store.count_votes_by_agent(
        session, tenant_id=tenant_id, since=since, until=until
    ):
        row = found.setdefault(agent_id, AgentFeedback(agent_id))
        if vote == "up":
            row.up += count
        else:
            row.down += count
    for agent_id, reason, count in await store.count_reasons_by_agent(
        session, tenant_id=tenant_id, since=since, until=until
    ):
        if reason in FEEDBACK_REASONS:
            row = found.setdefault(agent_id, AgentFeedback(agent_id))
            row.reasons[reason] = row.reasons.get(reason, 0) + count
    return sorted(found.values(), key=lambda r: (-(r.up + r.down), r.ma_agent_id or ""))


def shown(count: int) -> int | None:
    """The count, or None when one or two votes would point to the voters. Pure."""
    return None if 0 < count < MIN_SHOWN else count


class ReasonCount(BaseModel):
    code: str
    label: str
    count: int | None


class AgentFeedbackOut(BaseModel):
    id: str | None
    name: str
    up: int | None
    down: int | None
    reasons: list[ReasonCount]


class FeedbackOut(BaseModel):
    days: int
    agents: list[AgentFeedbackOut]


def feedback_report(
    found: list[AgentFeedback], *, names: Mapping[str, str], days: int
) -> FeedbackOut:
    """`found` named by agent, with every count under `MIN_SHOWN` withheld. Pure."""
    return FeedbackOut(
        days=days,
        agents=[
            AgentFeedbackOut(
                id=f.ma_agent_id,
                name=names.get(f.ma_agent_id, UNKNOWN_AGENT) if f.ma_agent_id else UNKNOWN_AGENT,
                up=shown(f.up),
                down=shown(f.down),
                reasons=[
                    ReasonCount(code=code, label=label, count=shown(f.reasons[code]))
                    for code, label in FEEDBACK_REASONS.items()
                    if code in f.reasons
                ],
            )
            for f in found
        ],
    )
