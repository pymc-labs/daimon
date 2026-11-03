"""What people said about answers, counted by agent: thumbs and the reasons picked.

Counts only. The text a person wrote, the message and who voted never leave the store
through here, so an admin reads how an agent is doing, not who said what.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from daimon.core.authz import Subject
from daimon.core.errors import UserFacingError
from daimon.core.message_feedback import FEEDBACK_REASONS
from daimon.core.stores import message_feedback as store
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["AgentFeedback", "FeedbackNotAllowed", "feedback_summary"]


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
