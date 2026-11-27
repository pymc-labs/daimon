"""Store for workflows, their published versions and their access list.

Specs are stored as the JSON `daimon.core.workflows.spec.dump_spec` gives; this store
does not interpret them. A draft save names the revision it began from and fails when
someone saved since, so two editors never overwrite each other silently.

Erasing an account (`erase_for_account`) deletes the private workflows it owns, with
their runs; leaves the shared ones it owns or published `needs_owner`; drops its access
rows and answers; and replaces it in runs' `triggered_by` with a tombstone. The ledger
keeps account ids only, so the chain still verifies.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, TypedDict, Unpack, cast

from daimon.core._models import (
    LeadIntakeConfig,
    Workflow,
    WorkflowAccess,
    WorkflowRun,
    WorkflowVersion,
    WorkflowWait,
    WorkflowWaitAnswer,
)
from daimon.core.stores import workflow_triggers
from daimon.core.workflows.state import merged, oversized
from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

WorkflowStatus = Literal["active", "paused", "needs_owner"]
Visibility = Literal["private", "shared"]
VersionSource = Literal["web", "chat", "import", "routine"]
AccessRole = Literal["editor", "runner", "viewer"]
#: What erasure leaves in a run's `triggered_by` for an erased person.
ERASED_PERSON: dict[str, Any] = {"kind": "person", "erased": True}


class MadeIn(TypedDict):
    """Where a workflow was made: the provenance a DM destination is checked against."""

    known: bool
    channel_id: str | None
    thread_id: str | None


class ConvertedFrom(TypedDict, total=False):
    """The routine a workflow was converted from (`routine_translation`), to convert it back:
    its row's fields the workflow does not hold, and `last`, its last fire (`at`, `status`,
    `text`) for the page and the failure streak's start before the first run."""

    routine_id: str
    source_routine_id: str | None
    made_in_channel_id: str | None
    created_at: str | None
    agent_id: str
    agent_name: str
    name: str | None
    owner: str | None
    owner_account_id: str | None
    to: dict[str, Any] | None
    #: The routine's saved channel: its spend's, and a thread destination's parent.
    channel_id: str | None
    fired_at: str | None
    finished_at: str | None
    error: str | None
    tail: str | None
    migrated_at: str | None
    last: dict[str, Any] | None


class _Row(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)


class WorkflowRow(_Row):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    home_channel_id: str | None
    visibility: Visibility
    status: WorkflowStatus
    owner_account_id: uuid.UUID | None
    live_version: int | None
    draft_spec: dict[str, Any]
    draft_revision: int
    draft_base_version: int | None
    draft_updated_by_account_id: uuid.UUID | None
    draft_updated_at: datetime
    run_counter: int
    monthly_cap_usd: Decimal | None
    state: dict[str, Any]
    state_updated_at: datetime | None
    made_in: MadeIn | None
    converted_from: ConvertedFrom | None
    created_at: datetime
    updated_at: datetime


class WorkflowVersionRow(_Row):
    workflow_id: uuid.UUID
    number: int
    spec: dict[str, Any]
    spec_hash: str
    note: str
    source: VersionSource
    published_by_account_id: uuid.UUID | None
    approved_by_account_id: uuid.UUID | None
    agent_tools: dict[str, list[str]] | None
    skills: dict[str, dict[str, str]] | None
    published_at: datetime


class WorkflowAccessRow(_Row):
    workflow_id: uuid.UUID
    account_id: uuid.UUID | None
    group_id: str | None
    role: AccessRole
    granted_by_account_id: uuid.UUID | None
    created_at: datetime


async def create_workflow(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_account_id: uuid.UUID,
    name: str,
    spec: dict[str, Any],
    home_channel_id: str | None = None,
    visibility: Visibility = "private",
    made_in: MadeIn | None = None,
    converted_from: ConvertedFrom | None = None,
) -> WorkflowRow:
    orm = Workflow(
        made_in=made_in,
        converted_from=converted_from,
        visibility=visibility,
        tenant_id=tenant_id,
        owner_account_id=owner_account_id,
        name=name,
        draft_spec=spec,
        draft_updated_by_account_id=owner_account_id,
        home_channel_id=home_channel_id,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return WorkflowRow.model_validate(orm)


async def get_workflow(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID, lock: bool = False
) -> WorkflowRow | None:
    """The workflow, or None; `lock` holds its row until the transaction ends."""
    stmt = select(Workflow).where(Workflow.tenant_id == tenant_id, Workflow.id == workflow_id)
    orm = (await session.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    return None if orm is None else WorkflowRow.model_validate(orm)


async def merge_state(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    patch: Mapping[str, JsonValue],
    now: datetime,
) -> list[str] | None:
    """Merge `patch` into the workflow's state under its row lock, so two finishes both
    land. Returns the keys touched, or None (nothing written) when the result is too large."""
    # The column, not the row object: a loaded Workflow would answer from the session's
    # earlier read, not from the finish that committed while this waited for the lock.
    current = await session.scalar(
        select(Workflow.state)
        .where(Workflow.tenant_id == tenant_id, Workflow.id == workflow_id)
        .with_for_update()
    )
    assert current is not None, "a run's workflow outlives it"
    state = merged(current, patch)
    if oversized(state):
        return None
    await session.execute(
        update(Workflow).where(Workflow.id == workflow_id).values(state=state, state_updated_at=now)
    )
    return sorted(patch)


async def clear_state(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID, now: datetime
) -> None:
    await session.execute(
        update(Workflow)
        .where(Workflow.tenant_id == tenant_id, Workflow.id == workflow_id)
        .values(state={}, state_updated_at=now)
    )


async def list_workflows(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[WorkflowRow]:
    """Every workflow of the workspace, by name; callers filter by access."""
    rows = await session.execute(
        select(Workflow).where(Workflow.tenant_id == tenant_id).order_by(Workflow.name)
    )
    return [WorkflowRow.model_validate(orm) for orm in rows.scalars()]


async def save_draft(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    base_revision: int,
    spec: dict[str, Any],
    account_id: uuid.UUID | None,
) -> WorkflowRow | None:
    """Replace the draft if it is still at `base_revision`; None when someone saved since."""
    orm = (
        await session.execute(
            update(Workflow)
            .where(
                Workflow.tenant_id == tenant_id,
                Workflow.id == workflow_id,
                Workflow.draft_revision == base_revision,
            )
            .values(
                draft_spec=spec,
                draft_revision=Workflow.draft_revision + 1,
                draft_updated_by_account_id=account_id,
                draft_updated_at=func.now(),
            )
            .returning(Workflow)
        )
    ).scalar_one_or_none()
    return None if orm is None else WorkflowRow.model_validate(orm)


class Settings(TypedDict, total=False):
    name: str
    visibility: Visibility
    home_channel_id: str | None
    monthly_cap_usd: Decimal | None
    owner_account_id: uuid.UUID | None
    #: Replaces the draft without a new revision: a rename kept in step with the settings.
    draft_spec: dict[str, Any]


async def update_settings(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    **changes: Unpack[Settings],
) -> None:
    """Change the fields given; versions and the draft's revision are not touched."""
    if changes:
        await session.execute(
            update(Workflow)
            .where(Workflow.tenant_id == tenant_id, Workflow.id == workflow_id)
            .values(**changes)
        )


async def set_status(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID, status: WorkflowStatus
) -> None:
    await session.execute(
        update(Workflow)
        .where(Workflow.tenant_id == tenant_id, Workflow.id == workflow_id)
        .values(status=status)
    )


async def is_live(
    session: AsyncSession,
    *,
    workflow_id: uuid.UUID,
    version: int,
    owner_account_id: uuid.UUID | None,
) -> bool:
    """Whether `version` is still the live one, under the same owner."""
    found = await session.scalar(
        select(Workflow.id).where(
            Workflow.id == workflow_id,
            Workflow.live_version == version,
            Workflow.owner_account_id.is_not_distinct_from(owner_account_id),
        )
    )
    return found is not None


async def pause_active(session: AsyncSession, *, workflow_id: uuid.UUID) -> bool:
    """Pause the workflow if it is active; whether this call paused it."""
    paused = await session.execute(
        update(Workflow)
        .where(Workflow.id == workflow_id, Workflow.status == "active")
        .values(status="paused")
        .returning(Workflow.id)
    )
    return paused.first() is not None


async def delete_workflow(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID
) -> bool:
    """Delete the workflow with all its state (FK cascades) and unbind lead intake from it;
    usage rows keep their money."""
    result = await session.execute(
        delete(Workflow)
        .where(Workflow.tenant_id == tenant_id, Workflow.id == workflow_id)
        .returning(Workflow.id)
    )
    gone = result.scalars().all()
    await _unbind_intake(session, gone)
    return bool(gone)


async def _unbind_intake(session: AsyncSession, workflow_ids: Sequence[uuid.UUID]) -> None:
    """Clear `IntakeConfig.workflow_id` where it names a deleted workflow."""
    if workflow_ids:
        config = LeadIntakeConfig.config
        await session.execute(
            update(LeadIntakeConfig)
            .where(config["workflow_id"].astext.in_([str(i) for i in workflow_ids]))
            .values(config=config.op("-", return_type=JSONB)("workflow_id"))
        )


async def publish_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    spec: dict[str, Any],
    spec_hash: str,
    source: VersionSource,
    published_by: uuid.UUID,
    note: str = "",
    approved_by: uuid.UUID | None = None,
    now: datetime | None = None,
    agent_tools: Mapping[str, Sequence[str]] | None = None,
    skills: Mapping[str, Mapping[str, str]] | None = None,
) -> WorkflowVersionRow:
    """Add the next version and make it live with its triggers; the draft now begins from it."""
    workflow = await get_workflow(session, tenant_id=tenant_id, workflow_id=workflow_id, lock=True)
    if workflow is None:
        raise LookupError(f"No workflow {workflow_id}")
    latest = await session.scalar(
        select(func.max(WorkflowVersion.number)).where(WorkflowVersion.workflow_id == workflow_id)
    )
    orm = WorkflowVersion(
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        number=(latest or 0) + 1,
        spec=spec,
        spec_hash=spec_hash,
        source=source,
        note=note,
        published_by_account_id=published_by,
        approved_by_account_id=approved_by,
        agent_tools={k: list(v) for k, v in agent_tools.items()} if agent_tools else None,
        skills={k: dict(v) for k, v in skills.items()} if skills else None,
    )
    session.add(orm)
    await session.flush()
    await session.execute(
        update(Workflow)
        .where(Workflow.id == workflow_id)
        .values(live_version=orm.number, draft_base_version=orm.number)
    )
    found: object = spec.get("triggers")
    triggers = cast("list[object]", found) if isinstance(found, list) else []
    await workflow_triggers.sync_live(
        session,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        triggers=[cast("dict[str, Any]", t) for t in triggers if isinstance(t, dict)],
        now=now or datetime.now(UTC),
    )
    await session.refresh(orm)
    return WorkflowVersionRow.model_validate(orm)


async def get_version(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID, number: int
) -> WorkflowVersionRow | None:
    orm = await session.get(WorkflowVersion, (workflow_id, number))
    return (
        None
        if orm is None or orm.tenant_id != tenant_id
        else WorkflowVersionRow.model_validate(orm)
    )


async def list_versions(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID
) -> list[WorkflowVersionRow]:
    """Every version, newest first."""
    rows = await session.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.tenant_id == tenant_id, WorkflowVersion.workflow_id == workflow_id)
        .order_by(WorkflowVersion.number.desc())
    )
    return [WorkflowVersionRow.model_validate(orm) for orm in rows.scalars()]


async def set_access(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    role: AccessRole,
    granted_by: uuid.UUID | None,
    account_id: uuid.UUID | None = None,
    group_id: str | None = None,
) -> None:
    """Give a person or a group a role; giving another role replaces the first."""
    if (account_id is None) == (group_id is None):
        raise ValueError("Give access to one person or one group")
    constraint = "uq_workflow_access_account" if account_id else "uq_workflow_access_group"
    await session.execute(
        insert(WorkflowAccess)
        .values(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            account_id=account_id,
            group_id=group_id,
            role=role,
            granted_by_account_id=granted_by,
        )
        .on_conflict_do_update(
            constraint=constraint, set_={"role": role, "granted_by_account_id": granted_by}
        )
    )


async def remove_access(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
    group_id: str | None = None,
) -> None:
    subject = (
        WorkflowAccess.account_id == account_id
        if account_id
        else WorkflowAccess.group_id == group_id
    )
    await session.execute(
        delete(WorkflowAccess).where(
            WorkflowAccess.tenant_id == tenant_id,
            WorkflowAccess.workflow_id == workflow_id,
            subject,
        )
    )


async def list_access(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID
) -> list[WorkflowAccessRow]:
    rows = await session.execute(
        select(WorkflowAccess)
        .where(WorkflowAccess.tenant_id == tenant_id, WorkflowAccess.workflow_id == workflow_id)
        .order_by(WorkflowAccess.created_at)
    )
    return [WorkflowAccessRow.model_validate(orm) for orm in rows.scalars()]


def _private_owned(account_id: uuid.UUID) -> ColumnElement[bool]:
    return (Workflow.owner_account_id == account_id) & (Workflow.visibility == "private")


def _shared_held(account_id: uuid.UUID) -> ColumnElement[bool]:
    published = select(WorkflowVersion.workflow_id).where(
        WorkflowVersion.published_by_account_id == account_id,
        WorkflowVersion.number == Workflow.live_version,
    )
    return (Workflow.visibility == "shared") & or_(
        Workflow.owner_account_id == account_id, Workflow.id.in_(published)
    )


def _triggered_by(account_id: uuid.UUID) -> ColumnElement[bool]:
    return WorkflowRun.triggered_by.contains({"account_id": str(account_id)})


async def count_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """What erasure changes: private workflows, shared ones held, access rows, answers and runs."""
    counts = [
        select(func.count()).where(_private_owned(account_id)),
        select(func.count()).where(_shared_held(account_id)),
        select(func.count()).where(WorkflowAccess.account_id == account_id),
        select(func.count()).where(WorkflowWaitAnswer.account_id == account_id),
        select(func.count()).where(WorkflowWait.decided_by_account_id == account_id),
        select(func.count()).where(_triggered_by(account_id)),
    ]
    return sum([int(await session.scalar(stmt) or 0) for stmt in counts])


async def erase_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Erase the person from every workspace's workflows (module docstring); returns the count."""
    erased = await session.execute(
        delete(Workflow).where(_private_owned(account_id)).returning(Workflow.id)
    )
    gone = erased.scalars().all()
    await _unbind_intake(session, gone)
    results = [
        await session.execute(
            update(Workflow)
            .where(_shared_held(account_id))
            .values(status="needs_owner")
            .returning(Workflow.id)
        ),
        await session.execute(
            delete(WorkflowAccess)
            .where(WorkflowAccess.account_id == account_id)
            .returning(WorkflowAccess.id)
        ),
        await session.execute(
            delete(WorkflowWaitAnswer)
            .where(WorkflowWaitAnswer.account_id == account_id)
            .returning(WorkflowWaitAnswer.id)
        ),
        await session.execute(
            update(WorkflowWait)
            .where(WorkflowWait.decided_by_account_id == account_id)
            .values(
                decided_by_account_id=None,
                comment=None,
                answer_text=None,
                answer_values=None,
                answer_files=None,
            )
            .returning(WorkflowWait.id)
        ),
        await session.execute(
            update(WorkflowRun)
            .where(_triggered_by(account_id))
            .values(triggered_by=ERASED_PERSON)
            .returning(WorkflowRun.id)
        ),
    ]
    await session.execute(
        update(Workflow)
        .where(Workflow.owner_account_id == account_id)
        .values(owner_account_id=None)
    )
    return len(gone) + sum(len(r.all()) for r in results)
