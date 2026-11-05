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
from daimon.core.feedback_summary import (
    FeedbackNotAllowed,
    FeedbackOut,
    feedback_report,
    feedback_summary,
)
from fastapi import APIRouter, HTTPException, Query


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
        return feedback_report(found, names=names, days=days)

    return router
