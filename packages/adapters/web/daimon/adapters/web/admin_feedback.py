"""`/api/admin/feedback`: thumbs and reasons per agent over a period, counts only.

A count of one or two is withheld (null): it could point to who voted.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from daimon.adapters.web.admin_common import AuditViewer, listed_agents
from daimon.adapters.web.caller import NOT_ALLOWED
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.feedback_summary import FeedbackNotAllowed, feedback_summary
from daimon.core.message_feedback import FEEDBACK_REASONS
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

UNKNOWN_AGENT = "Unknown agent"
MIN_SHOWN = 3


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


def feedback_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin")

    @router.get("/feedback")
    async def feedback(  # pyright: ignore[reportUnusedFunction]
        caller: AuditViewer, days: Annotated[int, Query(ge=1, le=365)] = 30
    ) -> FeedbackOut:
        tenant_id = caller.person.tenant_id
        until = runtime.now()
        names = {
            a.id: str(a.metadata.get(MA_METADATA_KEY_NAME) or a.name)
            for a in await listed_agents(runtime, tenant_id)
        }
        async with runtime.sessionmaker() as db:
            subject = (await caller.principal(db)).subject
            try:
                found = await feedback_summary(
                    db,
                    subject=subject,
                    tenant_id=tenant_id,
                    since=until - timedelta(days=days),
                    until=until + timedelta(seconds=1),
                )
            except FeedbackNotAllowed as err:
                raise HTTPException(status_code=403, detail=NOT_ALLOWED) from err
        return FeedbackOut(
            days=days,
            agents=[
                AgentFeedbackOut(
                    id=f.ma_agent_id,
                    name=names.get(f.ma_agent_id, UNKNOWN_AGENT)
                    if f.ma_agent_id
                    else UNKNOWN_AGENT,
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

    return router
