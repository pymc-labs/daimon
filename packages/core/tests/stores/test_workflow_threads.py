"""The thread store: a title in a channel names one thread, the first one recorded."""

from __future__ import annotations

from daimon.core.stores import workflow_threads, workflows
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_the_first_thread_recorded_for_a_title_wins(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    owner = await make_account(db_session, tenant=tenant)
    flow = await workflows.create_workflow(
        db_session,
        tenant_id=tenant.id,
        owner_account_id=owner.id,
        name="Digest",
        spec={"schema": "daimon.workflow/v1", "name": "Digest", "start": "done", "nodes": {}},
    )

    async def find(title: str) -> str | None:
        return await workflow_threads.find_thread(
            db_session, workflow_id=flow.id, platform="slack", channel_id="C1", title=title
        )

    async def record(thread_id: str) -> str:
        return await workflow_threads.record_thread(
            db_session,
            tenant_id=tenant.id,
            workflow_id=flow.id,
            platform="slack",
            channel_id="C1",
            title="Weekly",
            thread_id=thread_id,
        )

    assert await find("Weekly") is None
    assert (await record("t1"), await record("t2")) == ("t1", "t1"), (
        "a racing post reuses the thread already kept"
    )
    assert await find("Weekly") == "t1"
    assert await find("Other") is None
