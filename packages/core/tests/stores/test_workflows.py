"""The workflows store: drafts with revision checks, versions, access and erasure."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from daimon.core._models import UsageEvent, WorkflowRun
from daimon.core.stores import workflows
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

SPEC: dict[str, Any] = {
    "schema": "daimon.workflow/v1",
    "name": "Reply",
    "start": "done",
    "nodes": {},
}


async def test_a_stale_draft_save_is_refused_and_publishing_numbers_versions(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    owner = await make_account(db_session, tenant=tenant)
    flow = await workflows.create_workflow(
        db_session, tenant_id=tenant.id, owner_account_id=owner.id, name="Reply", spec=SPEC
    )
    saved = await workflows.save_draft(
        db_session,
        tenant_id=tenant.id,
        workflow_id=flow.id,
        base_revision=flow.draft_revision,
        spec=SPEC | {"name": "Two"},
        account_id=owner.id,
    )
    stale = await workflows.save_draft(
        db_session,
        tenant_id=tenant.id,
        workflow_id=flow.id,
        base_revision=flow.draft_revision,
        spec=SPEC,
        account_id=owner.id,
    )
    assert saved is not None and saved.draft_revision == flow.draft_revision + 1, "saved"
    assert stale is None, "a save from an old revision is refused"

    versions = [
        await workflows.publish_version(
            db_session,
            tenant_id=tenant.id,
            workflow_id=flow.id,
            spec=SPEC,
            spec_hash="h",
            source="web",
            published_by=owner.id,
        )
        for _ in range(2)
    ]
    live = await workflows.get_workflow(db_session, tenant_id=tenant.id, workflow_id=flow.id)
    numbers = [
        v.number
        for v in await workflows.list_versions(db_session, tenant_id=tenant.id, workflow_id=flow.id)
    ]
    assert versions[-1].number == 2 and numbers == [2, 1], "versions number up, newest first"
    assert live is not None and live.live_version == live.draft_base_version == 2, "live"


async def test_access_takes_one_role_per_person(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    owner = await make_account(db_session, tenant=tenant)
    editor = await make_account(db_session, tenant=tenant)
    flow = await workflows.create_workflow(
        db_session, tenant_id=tenant.id, owner_account_id=owner.id, name="Reply", spec=SPEC
    )
    t, w = tenant.id, flow.id
    for role in ("viewer", "editor"):
        await workflows.set_access(
            db_session,
            tenant_id=t,
            workflow_id=w,
            role=role,
            granted_by=owner.id,
            account_id=editor.id,
        )
    await workflows.set_access(
        db_session, tenant_id=t, workflow_id=w, role="runner", granted_by=None, group_id="ops"
    )

    access = await workflows.list_access(db_session, tenant_id=t, workflow_id=w)
    assert [(a.account_id, a.group_id, a.role) for a in access] == [
        (editor.id, None, "editor"),
        (None, "ops", "runner"),
    ], "a second grant replaces the person's role"
    await workflows.remove_access(db_session, tenant_id=t, workflow_id=w, group_id="ops")
    left = await workflows.list_access(db_session, tenant_id=t, workflow_id=w)
    assert len(left) == 1, "the group is removed"


async def test_erasure_deletes_private_workflows_and_hands_shared_ones_back(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    person = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    private = await workflows.create_workflow(
        db_session, tenant_id=tenant.id, owner_account_id=person.id, name="Mine", spec=SPEC
    )
    shared = await workflows.create_workflow(
        db_session,
        tenant_id=tenant.id,
        owner_account_id=other.id,
        name="Team",
        spec=SPEC,
        visibility="shared",
    )
    await workflows.publish_version(
        db_session,
        tenant_id=tenant.id,
        workflow_id=shared.id,
        spec=SPEC,
        spec_hash="h",
        source="web",
        published_by=person.id,
    )
    run = WorkflowRun(
        tenant_id=tenant.id,
        workflow_id=shared.id,
        version=1,
        number=1,
        triggered_by={"kind": "person", "account_id": str(person.id)},
        trigger_kind="manual",
        trigger_key="manual",
        budget_usd=Decimal(5),
    )
    db_session.add(run)
    await db_session.flush()
    usage = UsageEvent(
        tenant_id=tenant.id, managed_session_id="s", event_id="e", workflow_run_id=run.id
    )
    db_session.add(usage)
    await db_session.flush()

    counted = await workflows.count_for_account(db_session, account_id=person.id)
    erased = await workflows.erase_for_account(db_session, account_id=person.id)

    assert counted == erased == 3, "the private workflow, the shared one and the run"
    gone = await workflows.get_workflow(db_session, tenant_id=tenant.id, workflow_id=private.id)
    kept = await workflows.get_workflow(db_session, tenant_id=tenant.id, workflow_id=shared.id)
    assert gone is None, "a private workflow goes with its owner"
    assert kept is not None and kept.status == "needs_owner", "a shared one needs a new publisher"
    await db_session.refresh(run)
    assert run.triggered_by == workflows.ERASED_PERSON, "the run keeps a tombstone"

    await workflows.delete_workflow(db_session, tenant_id=tenant.id, workflow_id=shared.id)
    await db_session.refresh(usage)
    assert usage.workflow_run_id is None, "usage keeps its money when the run goes"
