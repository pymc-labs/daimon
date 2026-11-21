"""Workflow commands: what the web routes and the chat tools both do.

Each function takes an `Env` (who is asking, and the clock) and opens its own transaction.
The roles come from `access`; a refusal is a `WorkflowError` whose text is for the person
and names who can help. Runs start, stop and move only through `runs`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from daimon.core.config import Settings
from daimon.core.routine_files import RoutineFile
from daimon.core.stores import workflow_publish_requests as publish_requests
from daimon.core.stores import workflow_reads as reads
from daimon.core.stores import workflow_runs as runs_store
from daimon.core.stores import workflow_waits
from daimon.core.stores import workflows as store
from daimon.core.stores.workflow_run_events import list_events
from daimon.core.stores.workflow_runs import GrantRow, RunRow
from daimon.core.stores.workflows import WorkflowAccessRow, WorkflowRow
from daimon.core.workflows import context as turn_context
from daimon.core.workflows import exchange, gate, run_views, runs, views
from daimon.core.workflows.access import Rights, Who, holders, rights_of
from daimon.core.workflows.preflight import Place, Refusal, member_reads, private_reach
from daimon.core.workflows.runs import Sessions
from daimon.core.workflows.spec import Spec, dump_spec, node_hash, parse_spec, spec_hash
from daimon.core.workflows.taint import Catalog
from daimon.core.workflows.templates import load_template
from daimon.core.workflows.validate import Grant, Validation, validate_document
from daimon.core.workflows.views import obj
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

GONE = "This workflow is gone, or it is not shared with you."
RUN_GONE = "This run is gone, or you cannot see it."
_ACTIVE = ("queued", "running", "waiting")


class WorkflowError(Exception):
    """A refusal in words for the person."""


class NotFound(WorkflowError): ...


class Forbidden(WorkflowError): ...


class Invalid(WorkflowError): ...


class Refused(WorkflowError): ...


class Off(WorkflowError): ...


class Stale(WorkflowError):
    """The draft moved on since the caller read it; `draft` is what is there now."""

    def __init__(self, draft: views.Draft) -> None:
        super().__init__("Someone else changed this draft.")
        self.draft = draft


@dataclass(frozen=True)
class Env:
    sm: Sessions
    settings: Settings
    who: Who
    now: datetime
    catalog: Catalog | None = None


class Patch(BaseModel):
    name: str | None = None
    visibility: Literal["private", "shared"] | None = None
    monthly_cap_usd: float | None = None
    paused: bool | None = None
    editors: list[str] | None = None
    runners: list[str] | None = None


class NewWorkflow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: Literal["template", "blank", "import"] = Field(alias="from")
    template: str | None = None
    name: str | None = None
    yaml: str | None = None


async def ensure_on(env: Env) -> None:
    if not await gate.enabled(env.sm, env.settings, env.who.tenant_id):
        raise Off(gate.OFF)


# ---------- loading and rights ----------


async def _owner_name(session: AsyncSession, workflow: WorkflowRow) -> str:
    names = await views.people_of(session, workflow.tenant_id, [workflow.owner_account_id])
    return views.person(names, workflow.owner_account_id).name


async def _load(
    session: AsyncSession, env: Env, workflow_id: uuid.UUID, *, lock: bool = False
) -> tuple[WorkflowRow, list[WorkflowAccessRow], Rights]:
    workflow = await store.get_workflow(
        session, tenant_id=env.who.tenant_id, workflow_id=workflow_id, lock=lock
    )
    if workflow is None:
        raise NotFound(GONE)
    access = await store.list_access(session, tenant_id=workflow.tenant_id, workflow_id=workflow.id)
    rights = rights_of(workflow, access, env.who)
    if not rights.view:
        raise NotFound(GONE)
    return workflow, access, rights


async def _need(session: AsyncSession, workflow: WorkflowRow, allowed: bool, what: str) -> None:
    if not allowed:
        raise Forbidden(f"Only {what} can do this. Ask {await _owner_name(session, workflow)}.")


async def _budget(env: Env) -> float:
    """The workspace's default run budget; a spec over it needs an admin's grant."""
    features = await gate.features_at(env.sm, env.settings, env.who.tenant_id)
    return float(features.run_budget_usd) if features else 5.0


async def _view(
    env: Env,
    session: AsyncSession,
    workflow: WorkflowRow,
    access: Sequence[WorkflowAccessRow] | None = None,
    *,
    budget: float,
) -> views.Workflow:
    """`budget` is read before the transaction opens: its read takes a connection of its own."""
    return await views.workflow_view(
        session,
        workflow,
        env.who,
        now=env.now,
        default_budget_usd=budget,
        policy=env.settings.tool_safety,
        access=access,
    )


async def _validate(env: Env, spec: dict[str, Any]) -> Validation:
    budget = await _budget(env)
    return validate_document(
        spec, env.catalog, run_budget_usd=obj(spec.get("limits")).get("budget_usd") or budget
    )


def _sync(spec: dict[str, Any]) -> store.Settings:
    """The columns a spec names: its name, visibility and home channel."""
    changes: store.Settings = {}
    if isinstance(spec.get("name"), str) and spec["name"]:
        changes["name"] = spec["name"][:120]
    if spec.get("visibility") in ("private", "shared"):
        changes["visibility"] = spec["visibility"]
    home = obj(spec.get("home")).get("channel")
    if isinstance(home, str):
        changes["home_channel_id"] = home
    return changes


# ---------- workflows ----------


async def list_workflows(env: Env) -> list[views.WorkflowSummary]:
    async with env.sm() as session:
        return await views.list_summaries(
            session,
            env.who,
            now=env.now,
            default_budget_usd=await _budget(env),
            policy=env.settings.tool_safety,
        )


async def get(env: Env, workflow_id: uuid.UUID) -> views.Workflow:
    budget = await _budget(env)
    async with env.sm() as session:
        workflow, access, _ = await _load(session, env, workflow_id)
        return await _view(env, session, workflow, access=access, budget=budget)


BLANK: dict[str, Any] = {
    "schema": "daimon.workflow/v1",
    "triggers": [{"id": "manual", "kind": "manual"}],
    "start": "done",
    "nodes": {"done": {"kind": "finish", "title": "Done"}},
}


async def create(env: Env, body: NewWorkflow, defaults_root: Path) -> views.Workflow:
    match body.from_:
        case "template":
            spec = load_template(defaults_root, body.template or "")
            if spec is None:
                raise NotFound("There is no such starter workflow.")
        case "blank":
            name = (body.name or "").strip()
            if not name:
                raise Invalid("Give the workflow a name.")
            spec = {**BLANK, "name": name[:120]}
        case "import":
            try:
                spec = exchange.parse(body.yaml or "")
            except exchange.NotAWorkflow as err:
                raise Invalid(str(err)) from err
    columns = _sync(spec)
    budget = await _budget(env)
    async with env.sm.begin() as session:
        workflow = await store.create_workflow(
            session,
            tenant_id=env.who.tenant_id,
            owner_account_id=env.who.account_id,
            name=columns.get("name", "Untitled"),
            spec=spec,
            home_channel_id=columns.get("home_channel_id"),
            visibility=columns.get("visibility", "private"),
        )
        return await _view(env, session, workflow, budget=budget)


async def patch(env: Env, workflow_id: uuid.UUID, body: Patch) -> views.Workflow:
    budget = await _budget(env)
    async with env.sm.begin() as session:
        workflow, _, rights = await _load(session, env, workflow_id, lock=True)
        given = body.model_fields_set
        if given - {"name"}:
            await _need(session, workflow, rights.manage, "the owner or an admin")
        else:
            await _need(session, workflow, rights.edit, "the owner or an editor")
        changes: store.Settings = {}
        draft = dict(workflow.draft_spec)
        if body.name is not None:
            name = body.name.strip()
            if not name:
                raise Invalid("Give the workflow a name.")
            changes["name"], draft["name"] = name[:120], name[:120]
        if body.visibility is not None:
            changes["visibility"] = body.visibility
            if "visibility" in draft:
                draft["visibility"] = body.visibility
        if "monthly_cap_usd" in given:
            cap = body.monthly_cap_usd
            changes["monthly_cap_usd"] = None if cap is None else Decimal(str(cap))
        if draft != workflow.draft_spec:
            changes["draft_spec"] = draft
        await store.update_settings(
            session, tenant_id=workflow.tenant_id, workflow_id=workflow.id, **changes
        )
        if body.paused is not None:
            if workflow.live_version is None:
                raise Refused("Publish this workflow before pausing it.")
            await store.set_status(
                session,
                tenant_id=workflow.tenant_id,
                workflow_id=workflow.id,
                status="paused" if body.paused else "active",
            )
        if body.editors is not None:
            await _set_role(session, workflow, "editor", body.editors, env.who.account_id)
        if body.runners is not None:
            await _set_role(session, workflow, "runner", body.runners, env.who.account_id)
        fresh, access, _ = await _load(session, env, workflow_id)
        return await _view(env, session, fresh, access=access, budget=budget)


async def _set_role(
    session: AsyncSession,
    workflow: WorkflowRow,
    role: Literal["editor", "runner"],
    accounts: Sequence[str],
    granted_by: uuid.UUID,
) -> None:
    try:
        wanted = {uuid.UUID(a) for a in accounts}
    except ValueError as err:
        raise Invalid("Pick people from the list.") from err
    current = holders(
        await store.list_access(session, tenant_id=workflow.tenant_id, workflow_id=workflow.id),
        role,
    )
    for account_id in set(current) - wanted:
        await store.remove_access(
            session, tenant_id=workflow.tenant_id, workflow_id=workflow.id, account_id=account_id
        )
    for account_id in wanted - set(current):
        await store.set_access(
            session,
            tenant_id=workflow.tenant_id,
            workflow_id=workflow.id,
            role=role,
            granted_by=granted_by,
            account_id=account_id,
        )


async def delete(env: Env, workflow_id: uuid.UUID) -> None:
    async with env.sm.begin() as session:
        workflow, _, rights = await _load(session, env, workflow_id, lock=True)
        await _need(session, workflow, rights.manage, "the owner or an admin")
        if await runs_store.count_active_runs(session, workflow_id=workflow.id):
            raise Refused("This workflow has runs in progress. Cancel them first.")
        await store.delete_workflow(session, tenant_id=workflow.tenant_id, workflow_id=workflow.id)


# ---------- the draft ----------


async def _draft(env: Env, workflow: WorkflowRow) -> views.Draft:
    """The draft as shown; call it after your transaction ends, it opens its own."""
    validation = await _validate(env, workflow.draft_spec)
    async with env.sm() as session:
        # A spec with grants parsed when it was validated.
        new = (
            (await _held_grants(session, workflow, parse_spec(workflow.draft_spec), grants))[1]
            if (grants := validation.grants)
            else []
        )
        return await views.draft_view(session, workflow, validation, new)


async def get_draft(env: Env, workflow_id: uuid.UUID) -> views.Draft:
    async with env.sm() as session:
        workflow, _, _ = await _load(session, env, workflow_id)
    return await _draft(env, workflow)


async def context(
    env: Env, workflow_id: uuid.UUID, selected_node: str | None
) -> turn_context.WorkflowContext | None:
    """The workflow as Ask Daimon sees it; None when it is gone or no longer visible."""
    if not await gate.enabled(env.sm, env.settings, env.who.tenant_id):
        return None
    try:
        async with env.sm() as session:
            workflow, _, _ = await _load(session, env, workflow_id)
    except NotFound:
        return None
    problems = await _validate(env, workflow.draft_spec)
    node = obj(obj(workflow.draft_spec.get("nodes")).get(selected_node or ""))
    chosen = (
        turn_context.SelectedNode(
            id=str(selected_node),
            kind=str(node.get("kind", "")),
            title=turn_context.clip(str(node.get("title", ""))),
        )
        if selected_node and node
        else None
    )
    return turn_context.WorkflowContext(
        id=str(workflow.id),
        name=turn_context.clip(workflow.name),
        revision=workflow.draft_revision,
        live_version=workflow.live_version,
        status=views.status_of(workflow),
        errors=len(problems.errors),
        warnings=len(problems.warnings),
        selected_node=chosen,
    )


async def save_draft(
    env: Env, workflow_id: uuid.UUID, *, base_revision: int, spec: dict[str, Any]
) -> views.Draft:
    """Replace the draft. A draft may be invalid: it saves with its problems listed."""
    async with env.sm.begin() as session:
        workflow, _, rights = await _load(session, env, workflow_id)
        await _need(session, workflow, rights.edit, "the owner or an editor")
        saved = await store.save_draft(
            session,
            tenant_id=workflow.tenant_id,
            workflow_id=workflow.id,
            base_revision=base_revision,
            spec=spec,
            account_id=env.who.account_id,
        )
        current: WorkflowRow | None = None
        if saved is None:
            current = await store.get_workflow(
                session, tenant_id=workflow.tenant_id, workflow_id=workflow.id
            )
        else:
            name = _sync(spec).get("name")
            if name and name != workflow.name:
                await store.update_settings(
                    session, tenant_id=workflow.tenant_id, workflow_id=workflow.id, name=name
                )
    if saved is None:
        assert current is not None, "a refused save finds the workflow it names"
        raise Stale(await _draft(env, current))
    return await _draft(env, saved)


async def discard_draft(env: Env, workflow_id: uuid.UUID) -> views.Draft:
    """Back to the live version; a workflow never published keeps its draft."""
    async with env.sm.begin() as session:
        workflow, _, rights = await _load(session, env, workflow_id, lock=True)
        await _need(session, workflow, rights.edit, "the owner or an editor")
        if workflow.live_version is not None:
            await _copy_version(session, env, workflow, workflow.live_version)
        fresh = await store.get_workflow(
            session, tenant_id=workflow.tenant_id, workflow_id=workflow.id
        )
        assert fresh is not None
    return await _draft(env, fresh)


async def _copy_version(
    session: AsyncSession, env: Env, workflow: WorkflowRow, number: int
) -> None:
    version = await store.get_version(
        session, tenant_id=workflow.tenant_id, workflow_id=workflow.id, number=number
    )
    if version is None:
        raise NotFound("That version is gone.")
    saved = await store.save_draft(
        session,
        tenant_id=workflow.tenant_id,
        workflow_id=workflow.id,
        base_revision=workflow.draft_revision,
        spec=version.spec,
        account_id=env.who.account_id,
    )
    assert saved is not None, "the workflow row is locked"


async def validate(env: Env, workflow_id: uuid.UUID, spec: dict[str, Any]) -> Validation:
    async with env.sm() as session:
        await _load(session, env, workflow_id)
    return await _validate(env, spec)


# ---------- versions, publish, export ----------


async def versions(env: Env, workflow_id: uuid.UUID) -> list[views.Version]:
    async with env.sm() as session:
        workflow, _, _ = await _load(session, env, workflow_id)
        return await views.version_views(session, workflow)


async def version_spec(env: Env, workflow_id: uuid.UUID, number: int) -> views.VersionSpec:
    async with env.sm() as session:
        workflow, _, _ = await _load(session, env, workflow_id)
        row = await store.get_version(
            session, tenant_id=workflow.tenant_id, workflow_id=workflow.id, number=number
        )
        shown = next(
            (v for v in await views.version_views(session, workflow) if v.number == number), None
        )
        if row is None or shown is None:
            raise NotFound("That version is gone.")
        return views.VersionSpec(version=shown, spec=row.spec)


async def restore(env: Env, workflow_id: uuid.UUID, number: int) -> views.Draft:
    async with env.sm.begin() as session:
        workflow, _, rights = await _load(session, env, workflow_id, lock=True)
        await _need(session, workflow, rights.edit, "the owner or an editor")
        await _copy_version(session, env, workflow, number)
        fresh = await store.get_workflow(
            session, tenant_id=workflow.tenant_id, workflow_id=workflow.id
        )
        assert fresh is not None
    return await _draft(env, fresh)


async def publish(
    env: Env, workflow_id: uuid.UUID, note: str, *, source: Literal["web", "chat"] = "web"
) -> views.Workflow:
    budget = await _budget(env)
    async with env.sm.begin() as session:
        workflow, _, rights = await _load(session, env, workflow_id, lock=True)
        await _need(session, workflow, rights.edit, "the owner or an editor")
        if env.who.external:
            raise Forbidden("Publishing needs a person. Ask the workflow's owner to publish.")
        validation = await _validate(env, workflow.draft_spec)
        if validation.errors:
            first = validation.errors[0].message
            raise Invalid(f"Fix {len(validation.errors)} problem(s) before publishing: {first}")
        parsed = parse_spec(workflow.draft_spec)
        carried, new = await _held_grants(session, workflow, parsed, validation.grants)
        if new and not env.who.admin:
            raise Refused(
                f"This version needs an admin's approval for {len(new)} thing(s). "
                "Request approval instead."
            )
        place = Place(workflow.tenant_id, workflow, parsed, 0, "live", env.who.account_id)
        reads = member_reads(env.sm, env.settings, workflow.tenant_id)
        if (private := await private_reach(session, place, env.who.account_id, reads)) is not None:
            raise Forbidden(
                f"This workflow reaches {private}, which you cannot read. Ask an admin."
            )
        spec = dump_spec(parsed)
        row = await store.publish_version(
            session,
            tenant_id=workflow.tenant_id,
            workflow_id=workflow.id,
            spec=spec,
            spec_hash=spec_hash(parse_spec(spec)),
            source=source,
            published_by=env.who.account_id,
            note=note.strip()[:500],
            now=env.now,
        )
        for approver, grants in [
            *((g.approved_by_account_id, [_carried(g)]) for g in carried),
            (env.who.account_id, _grant_rows(parsed, [g.model_dump() for g in new], env.who)),
        ]:
            await publish_requests.add_grants(
                session,
                tenant_id=workflow.tenant_id,
                workflow_id=workflow.id,
                version=row.number,
                grants=grants,
                approved_by=approver or env.who.account_id,
                now=env.now,
            )
        changes = _sync(spec)
        if workflow.status == "needs_owner":
            changes["owner_account_id"] = env.who.account_id
            await store.set_status(
                session, tenant_id=workflow.tenant_id, workflow_id=workflow.id, status="active"
            )
        await store.update_settings(
            session, tenant_id=workflow.tenant_id, workflow_id=workflow.id, **changes
        )
        fresh, access, _ = await _load(session, env, workflow_id)
        view = await _view(env, session, fresh, access=access, budget=budget)
        return view


def _grant_rows(
    spec: Spec, grants: Sequence[Mapping[str, Any]], publisher: Who | uuid.UUID
) -> list[dict[str, Any]]:
    """Grant rows pinned to the node as published and to its publisher."""
    by = publisher.account_id if isinstance(publisher, Who) else publisher
    return [
        {
            "kind": g["kind"],
            "node_id": g.get("node"),
            "pins": {"published_by": str(by)}
            | (
                {"node_hash": node_hash(spec.nodes[g["node"]])}
                if g.get("node") in spec.nodes
                else {}
            ),
        }
        for g in grants
    ]


def _carried(grant: GrantRow) -> dict[str, Any]:
    return {
        "kind": grant.kind,
        "node_id": grant.node_id,
        "tool": grant.tool,
        "params": grant.params,
        "pins": grant.pins,
    }


async def _held_grants(
    session: AsyncSession, workflow: WorkflowRow, spec: Spec, needed: Sequence[Grant]
) -> tuple[list[GrantRow], list[Grant]]:
    """Of the grants a spec needs, those the live version holds for the same unchanged node
    (carried over), and those an admin must still approve."""
    held = (
        await runs_store.live_grants(
            session, workflow_id=workflow.id, version=workflow.live_version
        )
        if workflow.live_version
        else []
    )
    carried: list[GrantRow] = []
    new: list[Grant] = []
    for grant in needed:
        pinned = node_hash(spec.nodes[grant.node]) if grant.node in spec.nodes else None
        match = next(
            (
                g
                for g in held
                if g.kind == grant.kind
                and g.node_id == grant.node
                and (g.pins or {}).get("node_hash") == pinned
            ),
            None,
        )
        if match is None:
            new.append(grant)
        else:
            carried.append(match)
    return carried, new


async def request_publish(env: Env, workflow_id: uuid.UUID, note: str) -> views.Workflow:
    budget = await _budget(env)
    async with env.sm.begin() as session:
        workflow, _, rights = await _load(session, env, workflow_id, lock=True)
        await _need(session, workflow, rights.edit, "the owner or an editor")
        validation = await _validate(env, workflow.draft_spec)
        if validation.errors:
            raise Invalid(
                f"Fix {len(validation.errors)} problem(s) first: {validation.errors[0].message}"
            )
        _, new = await _held_grants(
            session, workflow, parse_spec(workflow.draft_spec), validation.grants
        )
        if not new:
            raise Refused("Nothing here needs approval. Publish it.")
        await publish_requests.open_request(
            session,
            tenant_id=workflow.tenant_id,
            workflow_id=workflow.id,
            revision=workflow.draft_revision,
            spec=workflow.draft_spec,
            note=note.strip()[:500],
            grants=[g.model_dump() for g in validation.grants],
            requested_by=env.who.account_id,
        )
        fresh, access, _ = await _load(session, env, workflow_id)
        return await _view(env, session, fresh, access=access, budget=budget)


async def decide_publish_request(
    env: Env, request_id: uuid.UUID, *, approve: bool, comment: str | None
) -> publish_requests.PublishRequestRow:
    """A current human admin approves (publishing the requested spec with its grants, as
    its requester) or rejects with a comment."""
    if not env.who.admin or env.who.external:
        raise Forbidden("Only an admin can approve this. Ask an admin.")
    note = (comment or "").strip()[:500] or None
    if not approve and note is None:
        raise Invalid("Add a comment.")
    async with env.sm.begin() as session:
        request = await publish_requests.get_request(
            session, tenant_id=env.who.tenant_id, request_id=request_id, lock=True
        )
        if request is None:
            raise NotFound("This request is gone.")
        if request.status != "pending":
            raise Refused("This request was already decided.")
        version = None
        if approve:
            workflow, _, _ = await _load(session, env, request.workflow_id, lock=True)
            parsed = parse_spec(request.spec)
            spec = dump_spec(parsed)
            requester = request.requested_by_account_id or env.who.account_id
            row = await store.publish_version(
                session,
                tenant_id=workflow.tenant_id,
                workflow_id=workflow.id,
                spec=spec,
                spec_hash=spec_hash(parsed),
                source="web",
                published_by=requester,
                note=request.note,
                approved_by=env.who.account_id,
                now=env.now,
            )
            version = row.number
            grants = _grant_rows(parsed, request.grants, requester)
            await publish_requests.add_grants(
                session,
                tenant_id=workflow.tenant_id,
                workflow_id=workflow.id,
                version=version,
                grants=grants,
                approved_by=env.who.account_id,
                now=env.now,
            )
            changes = _sync(spec)
            if workflow.status == "needs_owner":
                changes["owner_account_id"] = requester
                await store.set_status(
                    session, tenant_id=workflow.tenant_id, workflow_id=workflow.id, status="active"
                )
            await store.update_settings(
                session, tenant_id=workflow.tenant_id, workflow_id=workflow.id, **changes
            )
        await publish_requests.decide_request(
            session,
            request.id,
            status="approved" if approve else "rejected",
            by=env.who.account_id,
            comment=note,
            version=version,
            now=env.now,
        )
        decided = await publish_requests.get_request(
            session, tenant_id=env.who.tenant_id, request_id=request.id
        )
    assert decided is not None
    return decided


async def export(
    env: Env, workflow_id: uuid.UUID, fmt: exchange.Format, version: int | None
) -> tuple[str, str]:
    """The spec as a file: text and a file name. A version, or the draft when none is named."""
    async with env.sm() as session:
        workflow, _, _ = await _load(session, env, workflow_id)
        spec = workflow.draft_spec
        if version is not None:
            row = await store.get_version(
                session, tenant_id=workflow.tenant_id, workflow_id=workflow.id, number=version
            )
            if row is None:
                raise NotFound("That version is gone.")
            spec = row.spec
        return exchange.dump(spec, fmt), exchange.filename(
            str(spec.get("name") or workflow.name), fmt
        )


# ---------- runs ----------


async def _run(session: AsyncSession, env: Env, run_id: uuid.UUID) -> tuple[RunRow, WorkflowRow]:
    run = await runs_store.get_run(session, run_id, tenant_id=env.who.tenant_id)
    if run is None:
        raise NotFound(RUN_GONE)
    try:
        workflow, _, rights = await _load(session, env, run.workflow_id)
    except NotFound as err:
        raise NotFound(RUN_GONE) from err
    if not rights.sees_run(own=run_views.started_by_me(run, env.who.account_id)):
        raise NotFound(RUN_GONE)
    return run, workflow


async def list_runs(
    env: Env, workflow_id: uuid.UUID | None = None, *, limit: int = 100
) -> list[run_views.RunSummary]:
    async with env.sm() as session:
        if workflow_id is not None:
            _, _, _ = await _load(session, env, workflow_id)
        found = await reads.list_runs(
            session, tenant_id=env.who.tenant_id, workflow_id=workflow_id, limit=limit
        )
        known: dict[uuid.UUID, tuple[WorkflowRow, Rights]] = {}
        kept: list[RunRow] = []
        for run in found:
            if run.workflow_id not in known:
                try:
                    workflow, _, rights = await _load(session, env, run.workflow_id)
                except NotFound:
                    continue
                known[run.workflow_id] = (workflow, rights)
            if known[run.workflow_id][1].sees_run(
                own=run_views.started_by_me(run, env.who.account_id)
            ):
                kept.append(run)
        return await run_views.summaries(session, kept, {i: w.name for i, (w, _) in known.items()})


async def get_run(env: Env, run_id: uuid.UUID) -> run_views.RunDetail:
    async with env.sm() as session:
        run, workflow, _ = await _run(session, env, run_id)
        return await run_views.detail(session, run, workflow.name)


async def get_run_by_number(env: Env, workflow_id: uuid.UUID, number: int) -> run_views.RunDetail:
    async with env.sm() as session:
        await _load(session, env, workflow_id)
        run = await reads.run_by_number(
            session, tenant_id=env.who.tenant_id, workflow_id=workflow_id, number=number
        )
    if run is None:
        raise NotFound(RUN_GONE)
    return await get_run(env, run.id)


async def run_events(
    env: Env, run_id: uuid.UUID, after_seq: int = 0
) -> tuple[list[run_views.RunEvent], bool]:
    """The ledger rows after `after_seq`, and whether the run has ended."""
    async with env.sm() as session:
        run, _, _ = await _run(session, env, run_id)
        events = await run_views.event_views(session, run, after_seq=after_seq)
        return events, run.status not in _ACTIVE


async def verify_run(env: Env, run_id: uuid.UUID) -> run_views.Verify:
    async with env.sm() as session:
        run, _, _ = await _run(session, env, run_id)
        return run_views.verify(run, await list_events(session, run.id))


async def step(env: Env, run_id: uuid.UUID, key: str, attempt: int | None) -> run_views.StepDetail:
    async with env.sm() as session:
        run, _, _ = await _run(session, env, run_id)
        found = await run_views.step_detail(session, run, key, attempt, now=env.now)
    if found is None:
        raise NotFound("That step has not run.")
    return found


async def run_file(env: Env, run_id: uuid.UUID, file_id: str) -> RoutineFile:
    """A file the run holds, a step's or an answer's, for a viewer of the run to download."""
    async with env.sm() as session:
        run, _, _ = await _run(session, env, run_id)
        refs: list[Any] = []
        for step_row in await runs_store.list_steps(session, run.id):
            refs += step_row.files or []
            for wait in await workflow_waits.waits_of_step(session, step_row.id):
                refs += wait.answer_files or []
    for ref in refs:
        if isinstance(ref, dict) and ref.get("file_id") == file_id:  # pyright: ignore[reportUnknownMemberType]  # JSONB refs
            return RoutineFile.model_validate(ref)
    raise NotFound("There is no such file.")


def _refused(refusal: Refusal) -> WorkflowError:
    error = NotFound if refusal.reason == "not_found" else Refused
    return error(refusal.message)


async def _summary_of(env: Env, run: RunRow) -> run_views.RunSummary:
    async with env.sm() as session:
        workflow = await store.get_workflow(
            session, tenant_id=run.tenant_id, workflow_id=run.workflow_id
        )
        assert workflow is not None
        (summary,) = await run_views.summaries(session, [run], {workflow.id: workflow.name})
        return summary


async def _pins(env: Env, from_run: uuid.UUID, nodes: Sequence[str]) -> dict[str, dict[str, Any]]:
    async with env.sm() as session:
        run, _, _ = await _run(session, env, from_run)
        steps = await runs_store.list_steps(session, run.id)
    wanted = set(nodes)
    return {
        s.node_id: {"route": s.route, "output": s.output}
        for s in steps
        if s.node_id in wanted and s.status == "succeeded"
    }


async def start(
    env: Env,
    workflow_id: uuid.UUID,
    inputs: dict[str, Any],
    *,
    test: dict[str, Any] | None = None,
) -> run_views.RunSummary:
    """A manual run of the live version, or a test run of the draft (`test` options)."""
    async with env.sm() as session:
        workflow, _, rights = await _load(session, env, workflow_id)
    if test is not None:
        if not rights.edit:
            raise Forbidden("Only the owner or an editor can test a workflow.")
        if test.get("live_writes"):
            raise Refused("Test runs cannot write for real yet. Leave live writes off.")
    elif not rights.run:
        raise Forbidden("You can see this workflow but not run it. Ask its owner.")
    pinned_from = uuid.UUID(test["pinned_from"]) if test and test.get("pinned_from") else None
    pins = await _pins(env, pinned_from, test.get("pinned") or []) if pinned_from and test else {}
    # A manual run runs as whoever pressed Run, under the publisher's grants. Its inputs are
    # untrusted whoever typed them: the publisher's grants never vouch for a form.
    run_as = env.who.account_id
    result = await runs.start_run(
        env.sm,
        env.settings,
        tenant_id=workflow.tenant_id,
        start=runs.Start(
            workflow_id=workflow.id,
            run_as_account_id=run_as,
            triggered_by={"kind": "person", "account_id": str(env.who.account_id)},
            inputs=inputs,
            mode="test" if test is not None else "live",
            untrusted_inputs=True,
            pinned=pins,
            pinned_from=pinned_from,
        ),
        now=env.now,
    )
    if isinstance(result, Refusal):
        raise _refused(result)
    return await _summary_of(env, result)


async def _control(env: Env, run_id: uuid.UUID) -> tuple[RunRow, WorkflowRow]:
    async with env.sm() as session:
        run, workflow, rights = await _run(session, env, run_id)
    if not rights.controls_run(own=run_views.started_by_me(run, env.who.account_id)):
        raise Forbidden(
            "Only the owner or an editor can do this to a run. Ask the workflow's owner."
        )
    return run, workflow


async def cancel(env: Env, run_id: uuid.UUID) -> run_views.RunSummary:
    run, _ = await _control(env, run_id)
    if not await runs.cancel_run(
        env.sm, env.settings, run_id=run.id, account_id=env.who.account_id, now=env.now
    ):
        raise Refused("This run has already ended.")
    return await _fresh(env, run.id)


async def redrive(env: Env, run_id: uuid.UUID) -> run_views.RunSummary:
    run, _ = await _control(env, run_id)
    result = await runs.redrive(
        env.sm, env.settings, run_id=run.id, account_id=env.who.account_id, now=env.now
    )
    if isinstance(result, Refusal):
        raise _refused(result)
    return await _fresh(env, run.id)


async def decide(
    env: Env,
    run_id: uuid.UUID,
    decision: Literal["retry", "skip", "fail"],
    output: JsonValue = None,
) -> run_views.RunSummary:
    run, _ = await _control(env, run_id)
    if env.who.external:
        raise Forbidden("Deciding a step needs a person.")
    async with env.sm() as session:
        steps = await runs_store.list_steps(session, run.id)
    unknown = next((s for s in steps if s.status == "unknown"), None)
    if unknown is None or not await runs.decide_unknown(
        env.sm,
        env.settings,
        step_id=unknown.id,
        decision=decision,
        account_id=env.who.account_id,
        now=env.now,
        output=output,
    ):
        raise Refused("This step is no longer waiting.")
    return await _fresh(env, run.id)


async def rerun(
    env: Env,
    run_id: uuid.UUID,
    *,
    step: str | None = None,
    inputs: dict[str, Any] | None = None,
    version: str | int | None = None,
) -> run_views.RunSummary:
    """Run again, or from `step`: a new run that reuses the finished steps upstream of it."""
    run, workflow = await _control(env, run_id)
    test = version == "draft" or run.mode == "test"
    picked = version if isinstance(version, int) else None  # None: the fork's source version
    result = await runs.start_run(
        env.sm,
        env.settings,
        tenant_id=workflow.tenant_id,
        start=runs.Start(
            workflow_id=workflow.id,
            run_as_account_id=env.who.account_id,
            triggered_by={"kind": "person", "account_id": str(env.who.account_id)},
            inputs=inputs,
            mode="test" if test else "live",
            untrusted_inputs=True,
            fork_of=(run.id, step) if step else None,
            rerun_of=None if step else run.id,
            label=run.label,
            version=workflow.live_version if version == "live" else picked,
        ),
        now=env.now,
    )
    if isinstance(result, Refusal):
        raise _refused(result)
    return await _summary_of(env, result)


async def _fresh(env: Env, run_id: uuid.UUID) -> run_views.RunSummary:
    async with env.sm() as session:
        run = await runs_store.get_run(session, run_id, tenant_id=env.who.tenant_id)
    assert run is not None
    return await _summary_of(env, run)


class Attention(BaseModel):
    kind: str
    workflow: dict[str, str]
    run: dict[str, Any] | None
    step: str | None
    since: str
    expires_at: str | None
    detail: str


async def attention(env: Env) -> list[Attention]:
    """What waits for this person: failed or stuck runs and workflows without an owner."""
    async with env.sm() as session:
        out: list[Attention] = []
        latest_runs: dict[uuid.UUID, dict[str, str]] = {}
        for summary in await views.list_summaries(
            session,
            env.who,
            now=env.now,
            default_budget_usd=await _budget(env),
            policy=env.settings.tool_safety,
        ):
            ref = {"id": summary.id, "name": summary.name}
            workflow_id = uuid.UUID(summary.id)
            if summary.status in ("needs_owner", "needs_review"):
                out.append(
                    Attention(
                        kind=summary.status,
                        workflow=ref,
                        run=None,
                        step=None,
                        since=views.iso(env.now) or "",
                        expires_at=None,
                        detail="The owner left"
                        if summary.status == "needs_owner"
                        else "Changed grants",
                    )
                )
            latest_runs[workflow_id] = ref
        latest = await reads.recent_runs(
            session, tenant_id=env.who.tenant_id, workflow_ids=list(latest_runs), per=1
        )
        for workflow_id, found in latest.items():
            run = found[0]
            if run.status in ("failed", "timed_out"):
                _, _, rights = await _load(session, env, workflow_id)
                if rights.controls_run(own=run_views.started_by_me(run, env.who.account_id)):
                    out.append(
                        Attention(
                            kind="failed",
                            workflow=latest_runs[workflow_id],
                            run={"id": str(run.id), "number": run.number},
                            step=run.failure_step,
                            since=views.iso(run.ended_at or run.created_at) or "",
                            expires_at=None,
                            detail=run.failure_message or "The run failed.",
                        )
                    )
        for run in await reads.unknown_step_runs(session, tenant_id=env.who.tenant_id):
            try:
                workflow, _, rights = await _load(session, env, run.workflow_id)
            except NotFound:
                continue
            if rights.controls_run(own=run_views.started_by_me(run, env.who.account_id)):
                steps = await runs_store.list_steps(session, run.id)
                unknown = next((s for s in steps if s.status == "unknown"), None)
                out.append(
                    Attention(
                        kind="unknown",
                        workflow={"id": str(workflow.id), "name": workflow.name},
                        run={"id": str(run.id), "number": run.number},
                        step=unknown.node_id if unknown else run.failure_step,
                        since=views.iso(run.started_at or run.created_at) or "",
                        expires_at=None,
                        detail=(unknown and unknown.error_message)
                        or "A step may or may not have happened. Say what to do.",
                    )
                )
    return sorted(out, key=lambda a: a.since, reverse=True)
