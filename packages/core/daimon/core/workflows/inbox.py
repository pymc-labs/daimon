"""The web inbox: approvals and questions waiting on this person, and publish requests for
admins. Deciding a wait goes through `waits.decide_wait`; a publish request through
`service.decide_publish_request`."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Final, Literal, cast

from daimon.core.stores import workflow_proposals as proposals_store
from daimon.core.stores import workflow_publish_requests as publish_requests
from daimon.core.stores import workflow_runs as runs_store
from daimon.core.stores import workflow_waits as waits_store
from daimon.core.stores import workflows as store
from daimon.core.stores.workflow_publish_requests import PublishRequestRow
from daimon.core.stores.workflow_run_events import list_events
from daimon.core.stores.workflow_runs import RunRow
from daimon.core.stores.workflow_waits import Wait
from daimon.core.stores.workflows import WorkflowRow
from daimon.core.workflows import catalog, lanes, run_views, runs, service, views, waits
from daimon.core.workflows.service import Env
from daimon.core.workflows.spec import AgentNode, AnyNode, ApprovalNode, Spec
from daimon.core.workflows.views import Person, iso
from pydantic import BaseModel, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

KEPT: Final = timedelta(days=7)
#: Who asks when no agent does: approvals and the default agent's questions. An agent's
#: question or proposals name its key; the web shows the agent's name.
ASKED_BY: Final = "Daimon"
_NOTE: Final = {"type": "string", "title": "Note"}
_VERB: Final = {"approve": "Approved", "reject": "Rejected", "answer": "Answered"}
_PROPOSAL_ROUTES: Final = {waits.REJECT: "Reject all", waits.APPROVE: "Send selected"}

Kind = Literal["approval", "question", "publish", "proposals"]


class Decision(BaseModel):
    route: str
    label: str
    by: Person
    at: str
    comment: str | None
    selected: list[str] | None = None


class InboxItem(BaseModel):
    id: str
    kind: Kind
    title: str
    workflow: dict[str, str]
    run: dict[str, Any] | None
    step: str | None
    #: The step's title, else its key.
    step_title: str | None = None
    asked_by: str
    since: str
    expires_at: str | None
    decision: Decision | None


class Route(BaseModel):
    name: str
    label: str
    primary: bool
    comment: bool


class InboxDetail(InboxItem):
    show: str | None = None
    bound: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    question: str | None = None
    form: dict[str, Any] | None = None
    values: dict[str, Any] = {}
    routes: list[Route] = []
    publish: views.PublishRequest | None = None
    sent: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    answer: dict[str, Any] | None = None


class Decide(BaseModel):
    route: str
    comment: str | None = None
    selected: list[str] | None = None
    values: dict[str, JsonValue] = {}


def _label(route: str) -> str:
    return _VERB.get(route, route[:1].upper() + route[1:])


def routes(node: AnyNode | None, kind: str) -> list[Route]:
    names = waits.routes_of(node, kind)
    return [
        Route(
            name=n,
            label="Send answer"
            if n == waits.ANSWER
            else _PROPOSAL_ROUTES[n]
            if kind == waits.PROPOSALS
            else n[:1].upper() + n[1:],
            primary=i == len(names) - 1,
            comment=waits.needs_comment(node, kind, n),
        )
        for i, n in enumerate(names)
    ]


def _typed(node: AnyNode | None, wait: Wait) -> bool:
    return wait.kind == "question" or (
        isinstance(node, ApprovalNode) and node.answer in ("text", "form")
    )


def _question(node: AnyNode | None, wait: Wait, key: str) -> str:
    if wait.kind == "question":
        return str((wait.shown or {}).get("question", ""))
    return waits.step_title(node, key)


def _form(node: AnyNode | None, wait: Wait) -> dict[str, Any] | None:
    if isinstance(node, ApprovalNode) and node.form and node.answer not in ("text", "choice"):
        fields = {
            k: f.model_dump(by_alias=True, exclude_none=True) for k, f in (node.form or {}).items()
        }
        return {"type": "object", "properties": fields}
    if not _typed(node, wait):
        return None
    fields: dict[str, Any] = {"text": {"type": "string"}}
    if options := (wait.shown or {}).get("options"):
        fields = {"text": {"type": "string", "enum": list(options)}, "note": _NOTE}
    return {"type": "object", "required": ["text"], "properties": fields}


def _bound(preview: dict[str, Any], approved: dict[str, Any], spec: Spec) -> dict[str, Any]:
    """A bound Action as shown: the approved call once decided, else the preview."""
    call = approved.get(preview["node"]) or preview
    return {
        "tool": call["tool"],
        "label": catalog.tool_label(call["tool"]),
        "step": waits.step_title(spec.nodes.get(preview["node"]), preview["node"]),
        "args": call.get("args") or {},
    }


def _previews(shown: dict[str, Any]) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = shown.get("preview") or []
    return previews


def _in_words(arguments: dict[str, Any]) -> str:
    """A proposal's arguments on one line: `issue: 412, body: Thanks...`."""
    return ", ".join(
        f"{k}: {v if isinstance(v, str | int | float) else json.dumps(v)}"
        for k, v in arguments.items()
    )[:2000]


async def _proposals(session: AsyncSession, step_id: uuid.UUID) -> list[dict[str, Any]]:
    """Each proposal: the agent's reason as its title, then its arguments and `server/tool`."""
    return [
        {
            "id": str(p.id),
            "tool": p.tool,
            "title": p.reason or catalog.tool_label(p.tool),
            "detail": _in_words(p.arguments),
        }
        for p in await proposals_store.proposals_of(session, step_id)
    ]


def _answer(wait: Wait, names: dict[uuid.UUID, Person]) -> dict[str, Any] | None:
    if wait.status != "completed" or wait.decided_by_account_id is None:
        return None
    return run_views.answer_view(wait, iso(wait.closed_at) or "", names)


async def _sent(session: AsyncSession, wait: Wait) -> list[dict[str, Any]]:
    """Where the ask was posted, from the run's `wait.posted` rows."""
    posted = [
        cast("dict[str, Any]", e.data["sent"])
        for e in await list_events(session, wait.run_id)
        if e.type == "wait.posted"
        and e.data.get("id") == str(wait.id)
        and isinstance(e.data.get("sent"), dict)
    ]
    ids = [uuid.UUID(a) for p in posted if isinstance(a := p.get("addressee"), str)]
    names = await views.people_of(session, wait.tenant_id, ids)
    return [run_views.sent_view(p, names) for p in posted]


class _Seen:
    """The runs, specs and names a page of items reads, loaded once each."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.specs: dict[uuid.UUID, Spec] = {}
        self.workflows: dict[uuid.UUID, WorkflowRow | None] = {}

    async def spec(self, run: RunRow) -> Spec:
        if run.id not in self.specs:
            self.specs[run.id] = await runs.spec_of(self.session, run)
        return self.specs[run.id]

    async def workflow(self, tenant_id: uuid.UUID, workflow_id: uuid.UUID) -> WorkflowRow | None:
        if workflow_id not in self.workflows:
            self.workflows[workflow_id] = await store.get_workflow(
                self.session, tenant_id=tenant_id, workflow_id=workflow_id
            )
        return self.workflows[workflow_id]


async def _wait_detail(
    seen: _Seen, env: Env, wait: Wait, names: dict[uuid.UUID, Person]
) -> InboxDetail | None:
    run = await runs_store.get_run(seen.session, wait.run_id, tenant_id=env.who.tenant_id)
    step = await runs_store.get_step(seen.session, wait.step_id)
    if run is None or step is None or not await waits.answerable(seen.session, env.who, wait, run):
        return None
    workflow = await seen.workflow(run.tenant_id, run.workflow_id)
    spec = await seen.spec(run)
    node = spec.nodes.get(step.node_id)
    key = runs.key_of(step)
    kind = waits.kind_of(wait)
    typed = kind != waits.PROPOSALS and _typed(node, wait)
    decision = None
    shown = wait.shown or {}
    if wait.status == "completed" and wait.decision and wait.decided_by_account_id:
        decision = Decision(
            route=wait.decision,
            label=_label(wait.decision),
            by=views.person(names, wait.decided_by_account_id),
            at=iso(wait.closed_at) or "",
            comment=wait.comment,
            selected=shown.get("selected"),
        )
    return InboxDetail(
        id=str(wait.id),
        kind="question" if typed else "proposals" if kind == waits.PROPOSALS else "approval",
        title=_question(node, wait, key)[:120] if typed else waits.step_title(node, key),
        workflow={"id": str(run.workflow_id), "name": workflow.name if workflow else ""},
        run={"id": str(run.id), "number": run.number},
        step=key,
        step_title=waits.step_title(node, key),
        asked_by=(lanes.agent_of(spec, node) if isinstance(node, AgentNode) else None) or ASKED_BY,
        since=iso(wait.opened_at) or "",
        expires_at=iso(wait.expires_at),
        decision=decision,
        show=shown.get("show"),
        bound=[_bound(b, shown.get("binds") or {}, spec) for b in _previews(shown)],
        proposals=await _proposals(seen.session, step.id) if kind == waits.PROPOSALS else [],
        question=_question(node, wait, key) if typed else None,
        form=_form(node, wait),
        values=dict(shown.get("values") or {})
        or ({"text": shown["options"][0]} if typed and shown.get("options") else {}),
        routes=routes(node, kind),
        attachments=[
            run_views.file_view(run.id, f)
            for f in cast("list[object]", shown.get("attachments") or [])
        ],
        answer=_answer(wait, names),
    )


async def _request_detail(
    seen: _Seen, request: PublishRequestRow, names: dict[uuid.UUID, Person]
) -> InboxDetail | None:
    workflow = await seen.workflow(request.tenant_id, request.workflow_id)
    if workflow is None:
        return None
    view = views.request_view(request, workflow, names)
    decision = None
    if request.status != "pending" and view.decided_by is not None:
        route = "approve" if request.status == "approved" else "reject"
        decision = Decision(
            route=route,
            label=_label(route),
            by=view.decided_by,
            at=view.decided_at or "",
            comment=request.comment,
        )
    approve = "Approve and publish" if request.grants else "Approve"
    return InboxDetail(
        id=str(request.id),
        kind="publish",
        title=f"Publish v{view.version} of {workflow.name}",
        workflow=view.workflow,
        run=None,
        step=None,
        asked_by=view.by.name,
        since=view.at,
        expires_at=None,
        decision=decision,
        routes=[
            Route(name="reject", label="Reject", primary=False, comment=True),
            Route(name="approve", label=approve, primary=True, comment=False),
        ],
        publish=view,
    )


async def _names(
    session: AsyncSession, env: Env, waits_: list[Wait], requests: list[PublishRequestRow]
) -> dict[uuid.UUID, Person]:
    wanted = [w.decided_by_account_id for w in waits_]
    wanted += [r.requested_by_account_id for r in requests]
    wanted += [r.decided_by_account_id for r in requests]
    return await views.people_of(session, env.who.tenant_id, wanted)


async def _requests(session: AsyncSession, env: Env, since: datetime) -> list[PublishRequestRow]:
    if not env.who.admin:
        return []
    return await publish_requests.inbox_requests(
        session, tenant_id=env.who.tenant_id, decided_since=since
    )


def _item(detail: InboxDetail) -> InboxItem:
    return InboxItem.model_validate(detail.model_dump(include=set(InboxItem.model_fields)))


async def list_items(env: Env) -> list[InboxItem]:
    since = env.now - KEPT
    async with env.sm() as session:
        found = await waits_store.inbox_waits(
            session, tenant_id=env.who.tenant_id, closed_since=since
        )
        found = [w for w in found if w.status in ("waiting", "completed")]
        requests = await _requests(session, env, since)
        names = await _names(session, env, found, requests)
        seen = _Seen(session)
        out = [d for w in found if (d := await _wait_detail(seen, env, w, names))]
        out += [d for r in requests if (d := await _request_detail(seen, r, names))]
    return sorted((_item(d) for d in out), key=lambda i: i.since, reverse=True)


async def _detail(session: AsyncSession, env: Env, item_id: uuid.UUID) -> InboxDetail:
    seen = _Seen(session)
    wait = await waits_store.get_wait(session, item_id, tenant_id=env.who.tenant_id)
    if wait is not None and wait.kind in ("approval", "question"):
        names = await _names(session, env, [wait], [])
        if (detail := await _wait_detail(seen, env, wait, names)) is not None:
            return detail.model_copy(update={"sent": await _sent(session, wait)})
    elif env.who.admin and (
        request := await publish_requests.get_request(
            session, tenant_id=env.who.tenant_id, request_id=item_id
        )
    ):
        names = await _names(session, env, [], [request])
        if (detail := await _request_detail(seen, request, names)) is not None:
            return detail
    raise service.NotFound("There is nothing waiting here.")


async def item(env: Env, item_id: uuid.UUID) -> InboxDetail:
    async with env.sm() as session:
        return await _detail(session, env, item_id)


async def decide(env: Env, item_id: uuid.UUID, body: Decide) -> InboxItem:
    """Decide from the inbox or the run page; the same item reads decided everywhere."""
    current = await item(env, item_id)
    if current.decision is not None:
        raise service.Refused(f"Already decided by {current.decision.by.name}.")
    if body.route not in {r.name for r in current.routes}:
        raise service.Invalid(f"Unknown answer: {body.route}")
    if current.kind == "publish":
        await service.decide_publish_request(
            env, item_id, approve=body.route == "approve", comment=body.comment
        )
    else:
        answer = waits.Answer(
            route=body.route, comment=body.comment, values=body.values, selected=body.selected
        )
        try:
            await waits.decide_wait(
                env.sm, env.settings, env.who, item_id, answer, via="web", now=env.now
            )
        except waits.WaitGone as err:
            raise service.Refused(str(err)) from err
        except waits.NotYours as err:
            raise service.Forbidden(str(err)) from err
        except waits.BadAnswer as err:
            raise service.Invalid(str(err)) from err
    after = await item(env, item_id)
    if after.decision is None:  # one of several approvals: counted, not decided yet
        return _item(after).model_copy(
            update={
                "decision": Decision(
                    route=body.route,
                    label="Counted",
                    by=Person(id=str(env.who.account_id), name="You"),
                    at=iso(env.now) or "",
                    comment=body.comment,
                )
            }
        )
    return _item(after)


async def attention(env: Env) -> list[service.Attention]:
    """`service.attention` plus the approvals and questions this person may answer."""
    out = await service.attention(env)
    out += [
        service.Attention(
            kind=i.kind,
            workflow=i.workflow,
            run=i.run,
            step=i.step,
            since=i.since,
            expires_at=i.expires_at,
            detail=i.title,
        )
        for i in await list_items(env)
        if i.decision is None and i.kind != "publish"
    ]
    return sorted(out, key=lambda a: a.since, reverse=True)
