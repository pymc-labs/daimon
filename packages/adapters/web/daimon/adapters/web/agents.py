"""`/api/agents`: the roster, one agent, its draft, apply, history and revert."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from daimon.adapters.web.access import is_admin
from daimon.adapters.web.agent_access import (
    Creator,
    Editor,
    Viewer,
    actor_of,
    change_deps,
    edit_refusal,
    viewer_of,
)
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.channel_names import channel_names
from daimon.adapters.web.nodes import AgentNode, agent_nodes, build_nodes, tenant_workspace
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import agent_catalog, agent_changes, agent_editing, agent_skill_files, agent_view
from daimon.core.agent_changes import AgentChangeDeps, Change, Draft
from daimon.core.agent_editing import AgentEditError
from daimon.core.agent_identity import queue_agent_face
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.agent_view import (
    AgentConfigDoc,
    AgentSummary,
    AgentView,
    PersonRef,
    SkillDoc,
)
from daimon.core.billing import month_start
from daimon.core.constants import ALLOWED_MODEL_IDS, DEFAULT_AGENT_MODEL, MODEL_DISPLAY_NAMES
from daimon.core.defaults.ma_index import list_skills_lenient
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import list_skills_lenient_cached
from daimon.core.model_presets import ModelSelection
from daimon.core.pricing import AGENT_MODEL_PRICING
from daimon.core.specs import AgentSpec
from daimon.core.stores import agent_drafts as drafts_store
from daimon.core.stores import agent_mcp_credentials as credentials_store
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_people as people_store
from daimon.core.tenant_summary import load_tenant_summary
from fastapi import APIRouter, HTTPException
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import Provider
from pydantic import BaseModel, Field, StringConstraints, ValidationError


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
    """An agent with the connection nodes it has, or can add."""

    nodes: list[AgentNode]


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


class SkillFilesBody(BaseModel):
    """The whole file set of a skill. None content keeps that file as the current version has it."""

    skill_id: str | None = None
    version: str | None = None  # the version the editor loaded; unchanged files come from it
    files: dict[
        str, Annotated[str | None, StringConstraints(max_length=agent_skill_files.MAX_EDIT_BYTES)]
    ] = Field(min_length=1, max_length=100)


class TokenBody(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


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


def agents_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/agents")
    nodes = build_nodes(runtime.settings)

    def deps() -> AgentChangeDeps:
        return change_deps(runtime)

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
        async with runtime.sessionmaker() as db:
            tenant = await tenants_store.get_tenant(db, tenant_id)
        # Without details the view hides attached servers, so no node is known to be addable.
        found = (
            agent_nodes(
                nodes,
                server_urls={s.key: s.url for s in view.mcp_servers},
                workspace=tenant_workspace(tenant),
            )
            if view.can_view_details
            else []
        )
        return AgentDetail(**dict(view), nodes=found)

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
        platform, names = await channel_names(
            runtime, tenant_id, {c.channel_id for c in summary.channels}
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
            raise edit_refusal(exc) from exc
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
            raise edit_refusal(exc) from exc

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
            raise edit_refusal(exc) from exc

    @router.delete("/{agent_id}/draft")
    async def discard_draft(caller: Editor, agent_id: str) -> None:  # pyright: ignore[reportUnusedFunction]
        try:
            await agent_changes.discard_draft(deps(), actor=actor_of(caller), ma_agent_id=agent_id)
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.post("/{agent_id}/apply")
    async def apply(caller: Editor, agent_id: str, body: ApplyBody) -> AgentDetail:  # pyright: ignore[reportUnusedFunction]
        try:
            await agent_changes.apply_draft(
                deps(), actor=actor_of(caller), ma_agent_id=agent_id, note=body.note
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        runtime.agents.forget(caller.person.tenant_id)
        return await view_of(caller, agent_id)

    @router.get("/{agent_id}/skills/{skill_id}/files")
    async def skill_files(  # pyright: ignore[reportUnusedFunction]
        caller: Editor, agent_id: str, skill_id: str, version: str | None = None
    ) -> agent_skill_files.SkillFilesDoc:
        try:
            return await agent_skill_files.read_skill_files(
                deps().edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                skill_id=skill_id,
                version=version,
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    @router.post("/{agent_id}/skills")
    async def save_skill(  # pyright: ignore[reportUnusedFunction]
        caller: Editor, agent_id: str, body: SkillFilesBody
    ) -> SkillDoc:
        try:
            return await agent_skill_files.save_skill_files(
                deps().edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                skill_id=body.skill_id,
                version=body.version,
                files=body.files,
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    async def server_url(caller: WebCaller, agent_id: str, key: str) -> tuple[uuid.UUID, str]:
        """The agent's id as the credential store keys it, and the URL of its server ``key``."""
        d = deps()
        agent = await agent_editing.load_editable_agent(
            d.edit, actor=actor_of(caller), ma_agent_id=agent_id
        )
        server = next((s for s in agent.mcp_servers if s.name == key), None)
        if server is None:
            raise HTTPException(status_code=404, detail="No such server.")
        return derive_agent_uuid(
            tenant_id=caller.person.tenant_id, ma_agent_id=agent.id
        ), server.url

    @router.put("/{agent_id}/mcp/{key}/token")
    async def save_token(  # pyright: ignore[reportUnusedFunction]
        caller: Editor, agent_id: str, key: str, body: TokenBody
    ) -> dict[str, bool]:
        try:
            owner_id, url = await server_url(caller, agent_id, key)
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        await save_agent_mcp_credential(
            sessionmaker=runtime.sessionmaker,
            fernet=runtime.fernet,
            tenant_id=caller.person.tenant_id,
            agent_id=owner_id,
            mcp_server_url=url,
            plaintext_token=body.token.strip(),
        )
        return {"ok": True}

    @router.delete("/{agent_id}/mcp/{key}/token")
    async def remove_token(  # pyright: ignore[reportUnusedFunction]
        caller: Editor, agent_id: str, key: str
    ) -> dict[str, bool]:
        try:
            owner_id, url = await server_url(caller, agent_id, key)
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        async with runtime.sessionmaker.begin() as session:
            await credentials_store.delete_credential(
                session,
                tenant_id=caller.person.tenant_id,
                agent_id=owner_id,
                mcp_server_url=url,
            )
        return {"ok": True}

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
            raise edit_refusal(exc) from exc
        runtime.agents.forget(caller.person.tenant_id)
        return await view_of(caller, agent_id)

    return router
