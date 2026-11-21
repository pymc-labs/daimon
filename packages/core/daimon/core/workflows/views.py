"""What the workflows API returns for workflows, drafts and versions.

The shapes are the web client's types (`web/src/modules/workflows/types.ts`). Ids become
names here, at read time: the stores hold account ids only.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from daimon.core.stores import platform_names, web_people, workflow_triggers
from daimon.core.stores import workflow_publish_requests as publish_requests
from daimon.core.stores import workflow_reads as reads
from daimon.core.stores import workflows as workflows_store
from daimon.core.stores.workflow_runs import RunRow
from daimon.core.stores.workflows import WorkflowAccessRow, WorkflowRow, WorkflowVersionRow
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.workflows import preflight
from daimon.core.workflows.access import Rights, Who, holders, rights_of
from daimon.core.workflows.spec import parse_spec
from daimon.core.workflows.validate import Grant as ValidationGrant
from daimon.core.workflows.validate import Validation
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

Status = Literal["live", "paused", "draft", "needs_owner", "needs_review"]
#: How long a publish stays on the page as "just published".
RECENT_PUBLISH = timedelta(minutes=10)
TICKS = 10
GONE_PERSON = "Someone who left"
_TOP = (
    "name", "goal", "home", "visibility", "label", "inputs", "triggers", "concurrency",
    "defaults", "limits", "start",
)  # fmt: skip


class Person(BaseModel):
    id: str
    name: str


class Tick(BaseModel):
    number: int
    status: str
    at: str


class RoutineRef(BaseModel):
    id: str
    name: str


class Grant(BaseModel):
    kind: str
    node: str | None = None
    detail: str


class PublishRequest(BaseModel):
    id: str
    workflow: dict[str, str]
    version: int
    note: str
    by: Person
    at: str
    revision: int
    grants: list[Grant]
    status: str
    decided_by: Person | None
    decided_at: str | None
    comment: str | None


class Published(BaseModel):
    version: int
    by: Person
    at: str
    note: str
    approved_by: Person | None


class WorkflowSummary(BaseModel):
    id: str
    name: str
    goal: str
    status: Status
    owner: Person
    run_as: Person | None
    live_version: int | None
    draft_revision: int | None
    draft_changes: int
    triggers: list[dict[str, Any]]
    ticks: list[Tick]
    next_run: str | None
    spend_usd: float
    visibility: Literal["private", "shared"]
    mine: bool
    shared_with_me: bool
    failing: bool
    steps: int
    source_routine: RoutineRef | None


class Workflow(WorkflowSummary):
    spec: dict[str, Any]
    home_name: str
    editors: list[Person]
    runners: list[Person]
    viewers: list[Person]
    monthly_cap_usd: float | None
    can_edit: bool
    can_run: bool
    updated_at: str
    is_admin: bool
    pending_request: PublishRequest | None
    published: Published | None
    migration: dict[str, Any] | None
    run_budget_usd: float | None


class Change(BaseModel):
    node: str | None
    field: str | None = None
    effect: Literal["added", "changed", "removed"]


class Draft(BaseModel):
    revision: int
    base_version: int | None
    spec: dict[str, Any]
    changes: list[Change]
    validation: Validation
    #: Grants the draft needs that the live version does not hold for the same unchanged node:
    #: publishing waits on an admin for these.
    new_grants: list[Grant]
    updated_at: str
    updated_by: Person


class Version(BaseModel):
    number: int
    published_by: Person
    published_at: str
    note: str
    grants: list[str]
    runs: int
    live: bool


class VersionSpec(BaseModel):
    version: Version
    spec: dict[str, Any]


def obj(value: object) -> dict[str, Any]:
    """`value` when it is a JSON object, else an empty one: spec drafts may be any shape."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def seq(value: object) -> list[Any]:
    return cast("list[Any]", value) if isinstance(value, list) else []


def iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat()


def diff(base: Mapping[str, Any] | None, spec: Mapping[str, Any]) -> list[Change]:
    """What the draft changes against the live spec, as the canvas marks it."""
    nodes: Mapping[str, Any] = spec.get("nodes") or {}
    if base is None:
        return [Change(node=node, effect="added") for node in nodes]
    out = [
        Change(node=None, field=key, effect="changed")
        for key in _TOP
        if base.get(key) != spec.get(key)
    ]
    before: Mapping[str, Any] = base.get("nodes") or {}
    for node, body in nodes.items():
        if node not in before:
            out.append(Change(node=node, effect="added"))
        elif before[node] != body:
            out.append(Change(node=node, effect="changed"))
    out += [Change(node=node, effect="removed") for node in before if node not in nodes]
    return out


async def people_of(
    session: AsyncSession, tenant_id: uuid.UUID, account_ids: Collection[uuid.UUID | None]
) -> dict[uuid.UUID, Person]:
    """Each account's name; an account the web app does not know is left out."""
    wanted = {a for a in account_ids if a is not None}
    found = await web_people.get_people_for_accounts(
        session, tenant_id=tenant_id, account_ids=wanted
    )
    return {a: Person(id=str(a), name=p.display_name) for a, p in found.items()}


def person(people: Mapping[uuid.UUID, Person], account_id: uuid.UUID | None) -> Person:
    if account_id is None:
        return Person(id="", name=GONE_PERSON)
    return people.get(account_id) or Person(id=str(account_id), name=GONE_PERSON)


def status_of(workflow: WorkflowRow, *, unreviewed: bool = False) -> Status:
    """`unreviewed`: the live version lacks a grant it needs (revoked, or never approved)."""
    if workflow.status == "needs_owner":
        return "needs_owner"
    if workflow.live_version is None:
        return "draft"
    if unreviewed:
        return "needs_review"
    return "paused" if workflow.status == "paused" else "live"


async def unreviewed(
    session: AsyncSession,
    workflow: WorkflowRow,
    live: WorkflowVersionRow | None,
    budget: float,
    policy: ToolSafetyPolicy,
) -> bool:
    """The live version lacks a grant, the budget's over the workspace default `budget` too."""
    if live is None:
        return False
    spec = parse_spec(live.spec)
    missing = await preflight.missing_grant(session, workflow.id, live.number, spec, budget, policy)
    return missing is not None


def _failing(runs: Sequence[RunRow]) -> bool:
    ended = next((r for r in runs if r.status not in ("queued", "running", "waiting")), None)
    return ended is not None and ended.status in ("failed", "timed_out")


def _month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _live_spec(workflow: WorkflowRow, versions: Mapping[int, WorkflowVersionRow]) -> dict[str, Any]:
    live = versions.get(workflow.live_version) if workflow.live_version else None
    return live.spec if live is not None else workflow.draft_spec


async def _versions_of(
    session: AsyncSession, workflow: WorkflowRow
) -> dict[int, WorkflowVersionRow]:
    if workflow.live_version is None:
        return {}
    live = await workflows_store.get_version(
        session,
        tenant_id=workflow.tenant_id,
        workflow_id=workflow.id,
        number=workflow.live_version,
    )
    return {} if live is None else {live.number: live}


def _summary(
    workflow: WorkflowRow,
    *,
    live: WorkflowVersionRow | None,
    owner: Person,
    run_as: Person | None,
    runs: Sequence[RunRow],
    spend: float,
    who: Who,
    next_run: datetime | None = None,
    unreviewed: bool = False,
    origin: tuple[uuid.UUID, Mapping[str, Any]] | None = None,
) -> WorkflowSummary:
    spec = live.spec if live is not None else workflow.draft_spec
    changes = len(diff(live.spec if live else None, workflow.draft_spec)) if live else 0
    nodes: Mapping[str, Any] = spec.get("nodes") or {}
    mine = workflow.owner_account_id == who.account_id
    return WorkflowSummary(
        id=str(workflow.id),
        name=workflow.name,
        goal=str(spec.get("goal") or ""),
        status=status_of(workflow, unreviewed=unreviewed),
        owner=owner,
        run_as=run_as,
        live_version=workflow.live_version,
        draft_revision=workflow.draft_revision if live is None or changes else None,
        draft_changes=changes,
        triggers=list(spec.get("triggers") or []),
        ticks=[
            Tick(number=r.number, status=r.status, at=iso(r.started_at or r.created_at) or "")
            for r in reversed(runs[:TICKS])
        ],
        next_run=iso(next_run) if workflow.status == "active" else None,
        spend_usd=spend,
        visibility=workflow.visibility,
        mine=mine,
        shared_with_me=workflow.visibility == "shared" and not mine,
        failing=_failing(runs),
        steps=len(nodes),
        source_routine=None
        if origin is None
        else RoutineRef(id=str(origin[0]), name=str(origin[1].get("name") or "")),
    )


async def list_summaries(
    session: AsyncSession,
    who: Who,
    *,
    now: datetime,
    default_budget_usd: float,
    policy: ToolSafetyPolicy,
) -> list[WorkflowSummary]:
    """The workflows the caller may see, by name."""
    workflows = await workflows_store.list_workflows(session, tenant_id=who.tenant_id)
    seen: list[WorkflowRow] = []
    for workflow in workflows:
        access = await workflows_store.list_access(
            session, tenant_id=who.tenant_id, workflow_id=workflow.id
        )
        if rights_of(workflow, access, who).view:
            seen.append(workflow)
    ids = [w.id for w in seen]
    runs = await reads.recent_runs(session, tenant_id=who.tenant_id, workflow_ids=ids, per=TICKS)
    spend = await reads.spend_since(session, tenant_id=who.tenant_id, since=_month_start(now))
    lives: dict[uuid.UUID, WorkflowVersionRow] = {}
    for workflow in seen:
        for version in (await _versions_of(session, workflow)).values():
            lives[workflow.id] = version
    runners = {
        a
        for w in seen
        for a in (
            w.owner_account_id,
            lives[w.id].published_by_account_id if w.id in lives else None,
        )
    }
    names = await people_of(session, who.tenant_id, runners)
    fires = await workflow_triggers.next_fires(session, workflow_ids=ids)
    origins = await workflow_triggers.routine_origins(session, workflow_ids=ids)
    return [
        _summary(
            w,
            live=lives.get(w.id),
            owner=person(names, w.owner_account_id),
            run_as=person(names, lives[w.id].published_by_account_id)
            if w.id in lives and w.status != "needs_owner"
            else None,
            runs=runs.get(w.id, []),
            spend=float(spend.get(w.id, 0)),
            who=who,
            next_run=fires.get(w.id),
            unreviewed=await unreviewed(session, w, lives.get(w.id), default_budget_usd, policy),
            origin=origins.get(w.id),
        )
        for w in seen
    ]


def request_view(
    row: publish_requests.PublishRequestRow,
    workflow: WorkflowRow,
    names: Mapping[uuid.UUID, Person],
) -> PublishRequest:
    return PublishRequest(
        id=str(row.id),
        workflow={"id": str(workflow.id), "name": workflow.name},
        version=row.version or (workflow.live_version or 0) + 1,
        note=row.note,
        by=person(names, row.requested_by_account_id),
        at=iso(row.requested_at) or "",
        revision=row.revision,
        grants=[Grant.model_validate(g) for g in row.grants],
        status=row.status,
        decided_by=person(names, row.decided_by_account_id) if row.decided_by_account_id else None,
        decided_at=iso(row.decided_at),
        comment=row.comment,
    )


async def workflow_view(
    session: AsyncSession,
    workflow: WorkflowRow,
    who: Who,
    *,
    now: datetime,
    default_budget_usd: float,
    policy: ToolSafetyPolicy,
    access: Sequence[WorkflowAccessRow] | None = None,
) -> Workflow:
    """The workflow page's record, for a caller who may see it."""
    access = (
        access
        if access is not None
        else await workflows_store.list_access(
            session, tenant_id=workflow.tenant_id, workflow_id=workflow.id
        )
    )
    rights = rights_of(workflow, access, who)
    versions = await _versions_of(session, workflow)
    live = versions.get(workflow.live_version) if workflow.live_version else None
    runs = (
        await reads.recent_runs(
            session, tenant_id=workflow.tenant_id, workflow_ids=[workflow.id], per=TICKS
        )
    ).get(workflow.id, [])
    spend = (
        await reads.spend_since(session, tenant_id=workflow.tenant_id, since=_month_start(now))
    ).get(workflow.id, 0)
    request = await publish_requests.pending_request(
        session, tenant_id=workflow.tenant_id, workflow_id=workflow.id
    )
    holding = [
        workflow.owner_account_id,
        live.published_by_account_id if live else None,
        live.approved_by_account_id if live else None,
        workflow.draft_updated_by_account_id,
        *(r.account_id for r in access),
        *((request.requested_by_account_id,) if request else ()),
    ]
    names = await people_of(session, workflow.tenant_id, holding)
    owner = person(names, workflow.owner_account_id)
    run_as = (
        person(names, live.published_by_account_id)
        if live is not None and workflow.status != "needs_owner"
        else None
    )
    fires = await workflow_triggers.next_fires(session, workflow_ids=[workflow.id])
    origin = (await workflow_triggers.routine_origins(session, workflow_ids=[workflow.id])).get(
        workflow.id
    )
    base = _summary(
        workflow,
        live=live,
        owner=owner,
        run_as=run_as,
        runs=runs,
        spend=float(spend),
        who=who,
        next_run=fires.get(workflow.id),
        unreviewed=await unreviewed(session, workflow, live, default_budget_usd, policy),
        origin=origin,
    )
    spec = _live_spec(workflow, versions)
    home = str(obj(spec.get("home")).get("channel") or workflow.home_channel_id or "")
    tenant_names = await _channel_names(session, workflow, [home] if home else [])
    fresh = (
        live is not None
        and workflow.draft_updated_at <= live.published_at
        and now - live.published_at < RECENT_PUBLISH
    )
    limits = obj(spec.get("limits"))
    return Workflow(
        **base.model_dump(),
        spec=spec,
        home_name=tenant_names.get(home, ""),
        editors=[person(names, a) for a in holders(access, "editor")],
        runners=[person(names, a) for a in holders(access, "runner")],
        viewers=[person(names, a) for a in holders(access, "viewer")],
        monthly_cap_usd=None
        if workflow.monthly_cap_usd is None
        else float(workflow.monthly_cap_usd),
        can_edit=rights.edit,
        can_run=rights.run,
        updated_at=iso(max(workflow.updated_at, workflow.draft_updated_at)) or "",
        is_admin=who.admin,
        pending_request=request_view(request, workflow, names) if request else None,
        published=Published(
            version=live.number,
            by=person(names, live.published_by_account_id),
            at=iso(live.published_at) or "",
            note=live.note,
            approved_by=person(names, live.approved_by_account_id)
            if live.approved_by_account_id
            else None,
        )
        if live is not None and fresh
        else None,
        migration=None if origin is None else _migration(base.source_routine, origin[1]),
        run_budget_usd=limits.get("budget_usd"),
    )


def _migration(routine: RoutineRef | None, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The workflow page's "Migrated from routine" strip, from the move's snapshot."""
    ref = None if routine is None else routine.model_dump()
    return {
        "routine": ref,
        "migrated_at": snapshot.get("migrated_at"),
        "last": snapshot.get("last"),
    }


async def _channel_names(
    session: AsyncSession, workflow: WorkflowRow, channel_ids: list[str]
) -> dict[str, str]:
    if not channel_ids:
        return {}
    from daimon.core.stores import tenants

    tenant = await tenants.get_tenant(session, workflow.tenant_id)
    if tenant is None:
        return {}
    return await platform_names.get_channel_names(
        session, tenant_id=tenant.id, platform=tenant.platform, channel_ids=channel_ids
    )


async def draft_view(
    session: AsyncSession,
    workflow: WorkflowRow,
    validation: Validation,
    new_grants: list[ValidationGrant],
) -> Draft:
    versions = await _versions_of(session, workflow)
    live = versions.get(workflow.live_version) if workflow.live_version else None
    names = await people_of(session, workflow.tenant_id, [workflow.draft_updated_by_account_id])
    return Draft(
        revision=workflow.draft_revision,
        base_version=workflow.draft_base_version,
        spec=workflow.draft_spec,
        changes=diff(live.spec if live else None, workflow.draft_spec),
        validation=validation,
        new_grants=[Grant.model_validate(g.model_dump()) for g in new_grants],
        updated_at=iso(workflow.draft_updated_at) or "",
        updated_by=person(names, workflow.draft_updated_by_account_id),
    )


async def version_views(session: AsyncSession, workflow: WorkflowRow) -> list[Version]:
    rows = await workflows_store.list_versions(
        session, tenant_id=workflow.tenant_id, workflow_id=workflow.id
    )
    counts = await reads.runs_per_version(session, workflow_id=workflow.id)
    kinds = await reads.grant_kinds_by_version(session, workflow_id=workflow.id)
    names = await people_of(session, workflow.tenant_id, [r.published_by_account_id for r in rows])
    return [
        Version(
            number=r.number,
            published_by=person(names, r.published_by_account_id),
            published_at=iso(r.published_at) or "",
            note=r.note,
            grants=kinds.get(r.number, []),
            runs=counts.get(r.number, 0),
            live=r.number == workflow.live_version,
        )
        for r in rows
    ]


__all__ = ["Rights"]
