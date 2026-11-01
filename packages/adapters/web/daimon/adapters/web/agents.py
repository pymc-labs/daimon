"""`/api/agents`: the roster, one agent, its draft, apply, history and revert."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from daimon.adapters.web.access import is_admin
from daimon.adapters.web.apps import AgentApp, agent_apps
from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import agent_catalog, agent_changes, agent_editing, agent_view
from daimon.core.agent_changes import AgentChangeDeps, Change, Draft
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_identity import queue_agent_face
from daimon.core.agent_view import (
    AgentConfigDoc,
    AgentSummary,
    AgentView,
    AgentViewDeps,
    AgentViewer,
    PersonRef,
    SkillDoc,
)
from daimon.core.authz import Subject, Surface
from daimon.core.billing import month_start
from daimon.core.constants import ALLOWED_MODEL_IDS, DEFAULT_AGENT_MODEL, MODEL_DISPLAY_NAMES
from daimon.core.defaults.ma_index import list_skills_lenient
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import list_skills_lenient_cached
from daimon.core.model_presets import ModelSelection
from daimon.core.pricing import AGENT_MODEL_PRICING
from daimon.core.specs import AgentSpec
from daimon.core.stores import agent_drafts as drafts_store
from daimon.core.stores import platform_names
from daimon.core.stores import web_people as people_store
from daimon.core.stores.tenants import get_tenant
from daimon.core.tenant_summary import load_tenant_summary
from fastapi import APIRouter, Depends, HTTPException
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import Provider
from pydantic import BaseModel, ValidationError

Viewer = Annotated[WebCaller, Depends(requires("agents.view"))]
Editor = Annotated[WebCaller, Depends(requires("agents.edit"))]
Creator = Annotated[WebCaller, Depends(requires("agents.create"))]

_STATUS = {
    "forbidden": 403,
    "not_found": 404,
    "stale": 409,
    "conflict": 409,
    "taken": 409,
    "invalid": 422,
}


class CatalogModel(BaseModel):
    id: str
    label: str
    backend: Provider = DEFAULT_BACKEND
    in_usd: float
    out_usd: float


class ToolLabel(BaseModel):
    name: str
    label: str


class Catalog(BaseModel):
    models: list[CatalogModel]
    skills: list[SkillDoc]
    channels: list[agent_view.PlaceDoc]
    tools: list[ToolLabel]


class Template(BaseModel):
    id: str
    name: str
    purpose: str
    icon: str
    skills: int
    servers: int
    model: str


class AgentVersionDoc(BaseModel):
    version: int
    at: datetime
    by: PersonRef
    note: str | None
    changes: list[Change]


class AgentDetail(AgentView):
    """An agent with the embedded apps' views of it."""

    apps: list[AgentApp]


class CreateBody(BaseModel):
    name: str
    template: str = "blank"
    model: str | None = None


class DraftBody(BaseModel):
    base_version: int
    config: AgentConfigDoc


class ApplyBody(BaseModel):
    note: str | None = None


class RevertBody(BaseModel):
    version: int


_BLANK = Template(
    id="blank",
    name="Blank",
    purpose="",
    icon="square-dashed",
    skills=0,
    servers=0,
    model=MODEL_DISPLAY_NAMES.get(DEFAULT_AGENT_MODEL, DEFAULT_AGENT_MODEL),
)


def _model_id(model: ModelSelection) -> str:
    return model if isinstance(model, str) else model["id"]


def _template_doc(spec: AgentSpec) -> Template:
    model = _model_id(spec.model)
    return Template(
        id=spec.name,
        name=spec.name,
        purpose=spec.description or "",
        icon="sparkles",
        skills=len(spec.skills),
        servers=len(spec.mcp_servers or []),
        model=MODEL_DISPLAY_NAMES.get(model, model),
    )


def _refused(exc: AgentEditError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.kind, 400), detail=str(exc))


def agents_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/agents")

    def deps() -> AgentChangeDeps:
        if runtime.anthropic is None:
            raise HTTPException(status_code=503, detail="Agents are not available here.")
        public_url = runtime.settings.mcp.public_url
        base = str(public_url) if public_url is not None else None
        return AgentChangeDeps(
            edit=AgentEditDeps(
                client=runtime.anthropic,
                session_factory=runtime.sessionmaker,
                public_url=base,
                denied_tools=runtime.settings.tool_safety.denied,
                resolver_cache=runtime.resolver_cache,
                deployment_default=runtime.deployment_default,
            ),
            view=AgentViewDeps(
                client=runtime.anthropic,
                session_factory=runtime.sessionmaker,
                markup=runtime.settings.billing.markup,
                avatar_base_url=base,
                deployment_default=runtime.deployment_default,
                skill_descriptions=runtime.skill_descriptions,
                mcp_tools=runtime.mcp_tool_lister,
                fernet=runtime.fernet,
            ),
        )

    def actor_of(caller: WebCaller) -> AgentActor:
        account = caller.person.account_id
        return AgentActor(
            tenant_id=caller.person.tenant_id,
            account_id=account,
            subject=Subject(is_admin=is_admin(caller.role), platform_user_id=str(account)),
            surface=Surface.WEB,
        )

    def viewer_of(caller: WebCaller) -> AgentViewer:
        return AgentViewer(
            account_id=caller.person.account_id,
            is_admin=is_admin(caller.role),
            may_edit="agents.edit" in caller.capabilities,
        )

    async def editors(tenant_id: uuid.UUID, agent_ids: list[str]) -> dict[str, uuid.UUID]:
        """Who wrote each agent's current recorded version, where the web app wrote it."""
        async with runtime.sessionmaker() as session:
            latest = await drafts_store.latest_version_by_agent(
                session, tenant_id=tenant_id, agent_ids=agent_ids
            )
        out = {a: v.account_id for a, v in latest.items() if v.account_id is not None}
        return out

    async def view_of(caller: WebCaller, ma_agent_id: str) -> AgentDetail:
        tenant_id = caller.person.tenant_id
        view = await agent_view.load_agent_view(
            deps().view,
            tenant_id=tenant_id,
            ma_agent_id=ma_agent_id,
            viewer=viewer_of(caller),
            month_start=month_start(runtime.now()),
            updated_by=await editors(tenant_id, [ma_agent_id]),
        )
        if view is None:
            raise HTTPException(status_code=404, detail="No such agent.")
        mcp_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
        apps = agent_apps(
            runtime.web.apps, agent_id=str(mcp_id), server_urls=[s.url for s in view.mcp_servers]
        )
        return AgentDetail(**dict(view), apps=apps)

    @router.get("")
    async def list_agents(caller: Viewer) -> list[AgentSummary]:  # pyright: ignore[reportUnusedFunction]
        d = deps()
        tenant_id = caller.person.tenant_id
        agents = await runtime.agents.agents(d.view.client, tenant_id)
        facts = await agent_view.load_agent_facts(
            d.view,
            tenant_id=tenant_id,
            agents=agents,
            month_start=month_start(runtime.now()),
            updated_by=await editors(tenant_id, [a.id for a in agents]),
        )
        async with runtime.sessionmaker() as session:
            drafts = await drafts_store.list_drafts(
                session, tenant_id=tenant_id, account_id=caller.person.account_id
            )
        drafted = {row.agent_id for row in drafts}
        actor, viewer = actor_of(caller), viewer_of(caller)
        out: list[AgentSummary] = []
        for fact in facts:
            changes = 0
            if fact.agent.id in drafted:
                try:
                    draft = await agent_changes.load_draft(
                        d, actor=actor, agent=fact.agent, facts=fact
                    )
                except AgentEditError:
                    draft = None  # no longer theirs to change: the draft is not shown
                changes = len(draft.changes) if draft is not None else 0
            view = agent_view.build_agent_view(fact, viewer)
            out.append(
                agent_view.summary_of(
                    view,
                    draft_changes=changes,
                    runs_in=agent_view.runs_in_of(view.places or []),
                )
            )
        return out

    @router.get("/catalog")
    async def catalog(caller: Viewer) -> Catalog:  # pyright: ignore[reportUnusedFunction]
        d = deps()
        tenant_id = caller.person.tenant_id
        if runtime.resolver_cache is not None:
            rows, _truncated = await list_skills_lenient_cached(
                d.view.client, runtime.resolver_cache
            )
        else:
            rows, _truncated = await list_skills_lenient(d.view.client)
        async with runtime.sessionmaker() as session:
            summary = await load_tenant_summary(
                session,
                tenant_id=tenant_id,
                default=runtime.deployment_default,
                now=runtime.now(),
            )
            tenant = await get_tenant(session, tenant_id)
            platform = tenant.platform if tenant is not None else "discord"
            names = await platform_names.get_channel_names(
                session,
                tenant_id=tenant_id,
                platform=platform,
                channel_ids={c.channel_id for c in summary.channels},
            )
        return Catalog(
            models=[
                CatalogModel(
                    id=model,
                    label=MODEL_DISPLAY_NAMES.get(model, model),
                    in_usd=AGENT_MODEL_PRICING[model].input,
                    out_usd=AGENT_MODEL_PRICING[model].output,
                )
                for model in ALLOWED_MODEL_IDS
            ],
            skills=agent_catalog.skills_of(
                rows, tenant_id=tenant_id, descriptions=runtime.skill_descriptions
            ),
            channels=agent_catalog.channels_of(
                summary.channels,
                platform=platform,
                names=names,
                is_admin=is_admin(caller.role),
            ),
            tools=[ToolLabel(name=name, label=name) for name in agent_editing.BUILTIN_NAMES],
        )

    @router.get("/templates")
    async def templates(caller: Viewer) -> list[Template]:  # pyright: ignore[reportUnusedFunction]
        return [_BLANK, *(_template_doc(spec) for spec in runtime.agent_templates)]

    @router.post("")
    async def create(caller: Creator, body: CreateBody) -> AgentDetail:  # pyright: ignore[reportUnusedFunction]
        chosen = next((t for t in runtime.agent_templates if t.name == body.template), None)
        if chosen is None and body.template != _BLANK.id:
            raise HTTPException(status_code=404, detail="No such template.")
        d = deps()
        selection: ModelSelection = body.model or (
            chosen.model if chosen is not None else DEFAULT_AGENT_MODEL
        )
        refusal = agent_editing.unknown_model_refusal(selection)
        if refusal is not None:
            raise HTTPException(status_code=422, detail=refusal)
        denied = agent_editing.denied_builtins(d.edit.denied_tools)
        base_tools = list(chosen.tools or []) if chosen is not None else []
        try:
            spec = AgentSpec(
                name=body.name,
                model=selection,
                tools=agent_editing.forbidden_builtins_off(denied, base_tools)
                if denied
                else (base_tools or None),
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="That name can't be used.") from exc
        if chosen is not None:
            spec = spec.model_copy(
                update={
                    "description": chosen.description,
                    "system": chosen.system,
                    "mcp_servers": chosen.mcp_servers,
                    "skills": chosen.skills,
                    "skill_repos": chosen.skill_repos,
                }
            )
        try:
            ma_agent_id = await agent_editing.create_agent(
                d.edit, actor=actor_of(caller), spec=spec, owner=True
            )
        except AgentEditError as exc:
            raise _refused(exc) from exc
        runtime.agents.forget(caller.person.tenant_id)
        queue_agent_face(
            runtime.sessionmaker,
            tenant_id=caller.person.tenant_id,
            agent_name=spec.name,
            metadata=None,
            default_agent_name=runtime.deployment_default.agent_name,
        )
        return await view_of(caller, ma_agent_id)

    @router.get("/{agent_id}")
    async def get_agent(caller: Viewer, agent_id: str) -> AgentDetail:  # pyright: ignore[reportUnusedFunction]
        return await view_of(caller, agent_id)

    @router.get("/{agent_id}/draft")
    async def get_draft(caller: Editor, agent_id: str) -> Draft | None:  # pyright: ignore[reportUnusedFunction]
        d = deps()
        agent = await agent_view.fetch_tenant_agent(
            d.view, tenant_id=caller.person.tenant_id, ma_agent_id=agent_id
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="No such agent.")
        try:
            return await agent_changes.load_draft(d, actor=actor_of(caller), agent=agent)
        except AgentEditError as exc:
            raise _refused(exc) from exc

    @router.patch("/{agent_id}/draft")
    async def save_draft(caller: Editor, agent_id: str, body: DraftBody) -> Draft | None:  # pyright: ignore[reportUnusedFunction]
        try:
            return await agent_changes.save_draft(
                deps(),
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                base_version=body.base_version,
                config=body.config,
            )
        except AgentEditError as exc:
            raise _refused(exc) from exc

    @router.delete("/{agent_id}/draft")
    async def discard_draft(caller: Editor, agent_id: str) -> None:  # pyright: ignore[reportUnusedFunction]
        try:
            await agent_changes.discard_draft(deps(), actor=actor_of(caller), ma_agent_id=agent_id)
        except AgentEditError as exc:
            raise _refused(exc) from exc

    @router.post("/{agent_id}/apply")
    async def apply(caller: Editor, agent_id: str, body: ApplyBody) -> AgentDetail:  # pyright: ignore[reportUnusedFunction]
        try:
            await agent_changes.apply_draft(
                deps(), actor=actor_of(caller), ma_agent_id=agent_id, note=body.note
            )
        except AgentEditError as exc:
            raise _refused(exc) from exc
        runtime.agents.forget(caller.person.tenant_id)
        return await view_of(caller, agent_id)

    @router.get("/{agent_id}/versions")
    async def versions(caller: Viewer, agent_id: str) -> list[AgentVersionDoc]:  # pyright: ignore[reportUnusedFunction]
        view = await view_of(caller, agent_id)
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as session:
            rows = await drafts_store.list_versions(session, tenant_id=tenant_id, agent_id=view.id)
            people = {
                person.account_id: PersonRef(id=str(person.id), name=person.display_name)
                for person in await people_store.list_people(session, tenant_id=tenant_id)
            }
        # A place left out of a later version still shows in an earlier one's config.
        hidden = (
            set[str]()
            if is_admin(caller.role)
            else agent_changes.confidential_place_ids(
                AgentConfigDoc.model_validate(row.config) for row in rows
            )
        )
        out: list[AgentVersionDoc] = []
        for row in rows:
            changes = agent_changes.without_hidden_place_changes(
                [Change.model_validate(change) for change in row.changes], hidden
            )
            if not view.can_view_details:
                changes = [c.model_copy(update={"before": None, "after": None}) for c in changes]
            by = people.get(row.account_id) if row.account_id is not None else None
            out.append(
                AgentVersionDoc(
                    version=row.version,
                    at=row.at,
                    by=by or view.owner,
                    note=row.note,
                    changes=changes,
                )
            )
        return out

    @router.post("/{agent_id}/revert")
    async def revert(caller: Editor, agent_id: str, body: RevertBody) -> AgentDetail:  # pyright: ignore[reportUnusedFunction]
        try:
            await agent_changes.revert(
                deps(), actor=actor_of(caller), ma_agent_id=agent_id, version=body.version
            )
        except AgentEditError as exc:
            raise _refused(exc) from exc
        runtime.agents.forget(caller.person.tenant_id)
        return await view_of(caller, agent_id)

    return router
