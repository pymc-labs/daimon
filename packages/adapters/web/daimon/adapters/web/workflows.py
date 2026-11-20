"""`/api/workflows/*`: the workflows module on the real stores.

Every rule lives in `daimon.core.workflows.service`, the one the chat tools use too; this
module describes who is asking and shapes the answers. Reading needs `workflows.view`,
everything else `workflows.run`; what a person may do to one workflow comes from their role
on it. Inbox decisions go to whoever the wait names, whatever their role.
"""

from __future__ import annotations

import asyncio
import functools
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from typing import Annotated, Any, Literal

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.access import is_admin
from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.chat import chattable_agents
from daimon.adapters.web.chat_json import agent_json
from daimon.adapters.web.directory import chattable
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.routines import reachable_channels_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.stores import tenants
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.channel_directory import list_shared_channel_ids
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.web_identity import place_of, thread_key
from daimon.core.workflows import (
    catalog,
    convert,
    inbox,
    run_views,
    service,
    templates,
    views,
    webhooks,
)
from daimon.core.workflows.access import Who
from daimon.core.workflows.runs import run_channel
from daimon.core.workflows.service import Env, WorkflowError
from daimon.core.workflows.spec import slug
from daimon.core.workflows.taint import tool_writes
from daimon.core.workflows.validate import Validation
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

Viewer = Annotated[WebCaller, Depends(requires("workflows.view"))]
Member = Annotated[WebCaller, Depends(requires("workflows.run"))]

SOON = "This is not available yet."
ASK_TITLE = "Ask Daimon"
POLL_S = 2.0
PING_EVERY = 8
_STATUS: dict[type[WorkflowError], int] = {
    service.NotFound: 404,
    service.Forbidden: 403,
    service.Invalid: 422,
    service.Refused: 409,
    service.Off: 403,
}


def _http(error: WorkflowError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(type(error), 409), detail=str(error))


def speaks[**P, R](call: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R | JSONResponse]]:
    """Turn the service's refusals into the statuses the app reads; a stale draft answers 409
    with the draft that is there now."""

    @functools.wraps(call)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R | JSONResponse:
        try:
            return await call(*args, **kwargs)
        except service.Stale as stale:
            return JSONResponse(
                status_code=409, content={"detail": str(stale), "draft": stale.draft.model_dump()}
            )
        except WorkflowError as error:
            raise _http(error) from error

    return wrapped


class DraftPut(BaseModel):
    base_revision: int
    spec: dict[str, Any]


class Note(BaseModel):
    note: str = ""


class ValidateBody(BaseModel):
    spec: dict[str, Any]


class TestOptions(BaseModel):
    live_writes: bool = False
    pinned: list[str] = []
    pinned_from: str | None = None


class StartBody(BaseModel):
    inputs: dict[str, Any] = {}
    test: TestOptions | None = None


class DecideBody(BaseModel):
    decision: Literal["retry", "skip", "fail"]
    output: Any = None


class ForkBody(BaseModel):
    step: str | None = None
    inputs: dict[str, Any] | None = None
    version: str | int | None = Field(default=None)


_LISTEN_ERRORS = (OSError, SQLAlchemyError, asyncpg.PostgresError, asyncpg.InterfaceError)


def workflows_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/workflows")

    async def env_of(caller: WebCaller, *, with_catalog: bool = False) -> Env:
        person = caller.person
        who = Who(person.tenant_id, person.account_id, admin=is_admin(caller.role))
        env = Env(sm=runtime.sessionmaker, settings=runtime.settings, who=who, now=runtime.now())
        try:
            await service.ensure_on(env)
        except WorkflowError as error:
            raise _http(error) from error
        if with_catalog:
            options = await options_of(caller)
            env = Env(env.sm, env.settings, env.who, env.now, options.for_validation())
        return env

    def client() -> AsyncAnthropic | None:
        return runtime.anthropic or (runtime.turns.deps.anthropic if runtime.turns else None)

    async def options_of(caller: WebCaller) -> catalog.Options:
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            tenant = await tenants.get_tenant(db, tenant_id)
            platform = tenant.platform if tenant is not None else ""
            people = await catalog.people(db, tenant_id)
            shared = await list_shared_channel_ids(db, tenant_id=tenant_id, platform=platform)
            reachable = await reachable_channels_of(runtime, db, caller, platform)
            usable: list[BetaManagedAgentsAgent] = []
            if (anthropic := client()) is not None:
                listed = await runtime.agents.agents(anthropic, tenant_id)
                usable = await chattable(
                    db,
                    listed,
                    person=caller.person,
                    role=caller.role,
                    principal=await caller.principal(db),
                )
        agents: list[catalog.AgentOption] = []
        tools: dict[str, catalog.ToolOption] = {}
        agent_tools: dict[str, list[str]] = {}
        for agent in sorted(usable, key=lambda a: a.name.lower()):
            name = str(agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name)
            ids = [
                f"{t.mcp_server_name}/{c.name}"
                for t in agent.tools
                if t.type == "mcp_toolset"
                for c in t.configs
                if c.enabled is not False
            ]
            for tool_id in ids:
                tools.setdefault(
                    tool_id,
                    catalog.ToolOption(
                        id=tool_id,
                        label=tool_id.split("/")[1].replace("_", " ").capitalize(),
                        writes=tool_writes(tool_id, None),
                    ),
                )
            key = slug(name)
            agent_tools[key] = ids
            agents.append(catalog.AgentOption(key=key, id=agent.id, name=name, tools=len(ids)))
        return catalog.Options(
            agents=agents,
            tools=sorted(tools.values(), key=lambda t: t.id),
            people=people,
            channels=[
                catalog.ChannelOption(id=cid, name=name, platform=platform, shared=cid in shared)
                for cid, name in reachable.items()
            ],
            agent_tools=agent_tools,
        )

    # ----- not behind a workflow id: before `/{workflow_id}` -----

    @router.get("")
    @speaks
    async def list_all(caller: Viewer) -> list[views.WorkflowSummary]:  # pyright: ignore[reportUnusedFunction]
        return await service.list_workflows(await env_of(caller))

    @router.post("", status_code=201)
    @speaks
    async def create(body: service.NewWorkflow, caller: Member) -> views.Workflow:  # pyright: ignore[reportUnusedFunction]
        return await service.create(await env_of(caller), body, runtime.settings.defaults_root)

    @router.get("/templates")
    @speaks
    async def list_templates(caller: Viewer) -> list[templates.TemplateInfo]:  # pyright: ignore[reportUnusedFunction]
        await env_of(caller)
        return templates.list_templates(runtime.settings.defaults_root)

    @router.get("/catalog")
    @speaks
    async def get_catalog(caller: Viewer) -> catalog.Options:  # pyright: ignore[reportUnusedFunction]
        await env_of(caller)
        return await options_of(caller)

    @router.get("/attention")
    @speaks
    async def attention(caller: Viewer) -> list[service.Attention]:  # pyright: ignore[reportUnusedFunction]
        return await inbox.attention(await env_of(caller))

    @router.get("/runs")
    @speaks
    async def all_runs(caller: Viewer) -> list[run_views.RunSummary]:  # pyright: ignore[reportUnusedFunction]
        return await service.list_runs(await env_of(caller))

    @router.get("/runs/{run_id}")
    @speaks
    async def one_run(run_id: uuid.UUID, caller: Viewer) -> run_views.RunDetail:  # pyright: ignore[reportUnusedFunction]
        return await service.get_run(await env_of(caller), run_id)

    @router.get("/runs/{run_id}/events")
    @speaks
    async def run_events(run_id: uuid.UUID, caller: Viewer) -> list[run_views.RunEvent]:  # pyright: ignore[reportUnusedFunction]
        return (await service.run_events(await env_of(caller), run_id))[0]

    @router.get("/runs/{run_id}/verify")
    @speaks
    async def verify(run_id: uuid.UUID, caller: Viewer) -> run_views.Verify:  # pyright: ignore[reportUnusedFunction]
        return await service.verify_run(await env_of(caller), run_id)

    @router.get("/runs/{run_id}/steps/{key}")
    @speaks
    async def step(  # pyright: ignore[reportUnusedFunction]
        run_id: uuid.UUID, key: str, caller: Viewer, attempt: int | None = None
    ) -> run_views.StepDetail:
        return await service.step(await env_of(caller), run_id, key, attempt)

    @router.get("/runs/{run_id}/files/{name}")
    async def run_file(run_id: uuid.UUID, name: str, caller: Viewer) -> None:  # pyright: ignore[reportUnusedFunction]
        await env_of(caller)
        raise HTTPException(status_code=404, detail="There is no such file.")

    @asynccontextmanager
    async def wakeups(tenant_id: uuid.UUID, run_id: uuid.UUID) -> AsyncIterator[asyncio.Event]:
        """Set when the run's ledger changes; stays quiet where LISTEN is not possible."""
        woke = asyncio.Event()
        engine = runtime.sessionmaker.kw.get("bind")

        def on_notice(_conn: object, _pid: int, _channel: str, payload: str) -> None:
            if payload == str(run_id):
                woke.set()

        async with AsyncExitStack() as stack:
            try:
                if isinstance(engine, AsyncEngine):
                    conn = await stack.enter_async_context(engine.connect())
                    raw = (await conn.get_raw_connection()).driver_connection
                    if raw is not None:
                        channel = run_channel(tenant_id)
                        await raw.add_listener(channel, on_notice)
                        stack.push_async_callback(raw.remove_listener, channel, on_notice)
            except _LISTEN_ERRORS:
                pass
            yield woke

    async def frames(
        env: Env, run_id: uuid.UUID, after: int, first: list[run_views.RunEvent], ended: bool
    ) -> AsyncIterator[str]:
        async with wakeups(env.who.tenant_id, run_id) as woke:
            quiet = 0
            events = first
            while True:
                for event in events:
                    after = event.seq
                    yield f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"
                if ended and not events:
                    return
                quiet = 0 if events else quiet + 1
                if quiet and quiet % PING_EVERY == 0:
                    yield ": ping\n\n"
                with suppress(TimeoutError):
                    await asyncio.wait_for(woke.wait(), timeout=POLL_S)
                woke.clear()
                try:
                    events, ended = await service.run_events(env, run_id, after)
                except WorkflowError:
                    return

    @router.get("/runs/{run_id}/stream")
    @speaks
    async def stream(run_id: uuid.UUID, request: Request, caller: Viewer) -> StreamingResponse:  # pyright: ignore[reportUnusedFunction]
        """The ledger after `Last-Event-ID` (a sequence number), then live until the run ends."""
        env = await env_of(caller)
        raw = request.headers.get("last-event-id", "0")
        after = int(raw) if raw.isdigit() else 0
        first, ended = await service.run_events(env, run_id, after)
        return StreamingResponse(
            frames(env, run_id, after, first, ended),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.post("/runs/{run_id}/cancel")
    @speaks
    async def cancel(run_id: uuid.UUID, caller: Member) -> run_views.RunSummary:  # pyright: ignore[reportUnusedFunction]
        return await service.cancel(await env_of(caller), run_id)

    @router.post("/runs/{run_id}/redrive")
    @speaks
    async def redrive(run_id: uuid.UUID, caller: Member) -> run_views.RunSummary:  # pyright: ignore[reportUnusedFunction]
        return await service.redrive(await env_of(caller), run_id)

    @router.post("/runs/{run_id}/decide")
    @speaks
    async def decide(run_id: uuid.UUID, body: DecideBody, caller: Member) -> run_views.RunSummary:  # pyright: ignore[reportUnusedFunction]
        return await service.decide(await env_of(caller), run_id, body.decision, body.output)

    async def fork(run_id: uuid.UUID, body: ForkBody, caller: Member) -> run_views.RunSummary:
        return await service.rerun(
            await env_of(caller), run_id, step=body.step, inputs=body.inputs, version=body.version
        )

    router.add_api_route("/runs/{run_id}/rerun", speaks(fork), methods=["POST"])
    router.add_api_route("/runs/{run_id}/fork", speaks(fork), methods=["POST"])

    # ----- the approvals inbox -----

    @router.get("/inbox")
    @speaks
    async def inbox_list(caller: Viewer) -> list[inbox.InboxItem]:  # pyright: ignore[reportUnusedFunction]
        return await inbox.list_items(await env_of(caller))

    @router.get("/inbox/{item_id}")
    @speaks
    async def inbox_item(item_id: uuid.UUID, caller: Viewer) -> inbox.InboxDetail:  # pyright: ignore[reportUnusedFunction]
        return await inbox.item(await env_of(caller), item_id)

    @router.post("/inbox/{item_id}/decide")
    @speaks
    async def inbox_decide(  # pyright: ignore[reportUnusedFunction]
        item_id: uuid.UUID, body: inbox.Decide, caller: Viewer
    ) -> inbox.InboxItem:
        return await inbox.decide(await env_of(caller), item_id, body)

    # ----- moving routines -----

    @router.post("/from-routine/{routine_id}", status_code=201)
    @speaks
    async def from_routine(routine_id: uuid.UUID, caller: Member) -> views.Workflow:  # pyright: ignore[reportUnusedFunction]
        env = await env_of(caller)
        return await service.get(env, await convert.from_routine(env, routine_id))

    @router.post("/{workflow_id}/to-routine")
    @speaks
    async def to_routine(workflow_id: uuid.UUID, caller: Member) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"routine_id": str(await convert.to_routine(await env_of(caller), workflow_id))}

    # ----- one workflow -----

    @router.get("/{workflow_id}")
    @speaks
    async def get(workflow_id: uuid.UUID, caller: Viewer) -> views.Workflow:  # pyright: ignore[reportUnusedFunction]
        return await service.get(await env_of(caller), workflow_id)

    @router.patch("/{workflow_id}")
    @speaks
    async def patch(workflow_id: uuid.UUID, body: service.Patch, caller: Member) -> views.Workflow:  # pyright: ignore[reportUnusedFunction]
        return await service.patch(await env_of(caller), workflow_id, body)

    @router.delete("/{workflow_id}", status_code=204)
    @speaks
    async def delete(workflow_id: uuid.UUID, caller: Member) -> Response:  # pyright: ignore[reportUnusedFunction]
        await service.delete(await env_of(caller), workflow_id)
        return Response(status_code=204)

    @router.get("/{workflow_id}/draft")
    @speaks
    async def get_draft(workflow_id: uuid.UUID, caller: Viewer) -> views.Draft:  # pyright: ignore[reportUnusedFunction]
        return await service.get_draft(await env_of(caller), workflow_id)

    @router.put("/{workflow_id}/draft")
    @speaks
    async def put_draft(workflow_id: uuid.UUID, body: DraftPut, caller: Member) -> views.Draft:  # pyright: ignore[reportUnusedFunction]
        return await service.save_draft(
            await env_of(caller), workflow_id, base_revision=body.base_revision, spec=body.spec
        )

    @router.delete("/{workflow_id}/draft")
    @speaks
    async def discard(workflow_id: uuid.UUID, caller: Member) -> views.Draft:  # pyright: ignore[reportUnusedFunction]
        return await service.discard_draft(await env_of(caller), workflow_id)

    @router.post("/{workflow_id}/draft/publish")
    @speaks
    async def publish(workflow_id: uuid.UUID, body: Note, caller: Member) -> views.Workflow:  # pyright: ignore[reportUnusedFunction]
        return await service.publish(
            await env_of(caller, with_catalog=True), workflow_id, body.note
        )

    @router.post("/{workflow_id}/draft/request")
    @speaks
    async def request_publish(  # pyright: ignore[reportUnusedFunction]
        workflow_id: uuid.UUID, body: Note, caller: Member
    ) -> views.Workflow:
        return await service.request_publish(
            await env_of(caller, with_catalog=True), workflow_id, body.note
        )

    @router.post("/{workflow_id}/validate")
    @speaks
    async def validate(workflow_id: uuid.UUID, body: ValidateBody, caller: Viewer) -> Validation:  # pyright: ignore[reportUnusedFunction]
        return await service.validate(
            await env_of(caller, with_catalog=True), workflow_id, body.spec
        )

    @router.get("/{workflow_id}/chat")
    @speaks
    async def ask(workflow_id: uuid.UUID, caller: Member) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """The person's one Ask Daimon thread for this workflow, made on first use."""
        env = await env_of(caller)
        if not (await service.get(env, workflow_id)).can_edit:
            raise service.Forbidden(
                "Only the owner or an editor can ask Daimon to change this workflow."
            )
        agents = await chattable_agents(runtime, caller)
        person = caller.person
        subject = {"kind": "workflow", "id": str(workflow_id)}
        thread = None
        for _ in range(2):
            try:
                async with runtime.sessionmaker.begin() as db:
                    thread = await chat_store.get_subject_thread(
                        db, tenant_id=person.tenant_id, person_id=person.id, subject=subject
                    )
                    if thread is None:
                        if not agents:
                            raise service.Refused("No agent is available to you. Ask an admin.")
                        thread = await chat_store.create_thread(
                            db,
                            tenant_id=person.tenant_id,
                            person_id=person.id,
                            agent_id=agents[0].id,
                            agent_name=agents[0].name,
                            title=ASK_TITLE,
                            subject=subject,
                        )
                        await create_binding(
                            db,
                            tenant_id=thread.tenant_id,
                            platform="web",
                            parent_channel_id=place_of(person.id),
                            thread_id=thread_key(thread.id),
                            responder_ma_agent_id=thread.agent_id,
                            responder_name=thread.agent_name,
                            creator_account_id=person.account_id,
                            kind="web",
                        )
                break
            except IntegrityError:  # another tab made it first
                thread = None
        if thread is None:
            raise service.Refused("Try again in a moment.")
        agent = next((a for a in agents if a.id == thread.agent_id), None)
        avatars = await avatars_of(runtime, person.tenant_id, [agent] if agent else [])
        return {
            "session_id": str(thread.id),
            "agent": agent_json(agent, thread, avatar=avatars.get(thread.agent_id, "")),
        }

    @router.get("/{workflow_id}/export")
    @speaks
    async def export(  # pyright: ignore[reportUnusedFunction]
        workflow_id: uuid.UUID,
        caller: Viewer,
        format: Literal["yaml", "json"] = "yaml",
        version: int | None = None,
    ) -> Response:
        text, name = await service.export(await env_of(caller), workflow_id, format, version)
        return Response(
            text,
            media_type="application/json" if format == "json" else "text/yaml",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @router.get("/{workflow_id}/versions")
    @speaks
    async def versions(workflow_id: uuid.UUID, caller: Viewer) -> list[views.Version]:  # pyright: ignore[reportUnusedFunction]
        return await service.versions(await env_of(caller), workflow_id)

    @router.get("/{workflow_id}/versions/{number}")
    @speaks
    async def version(workflow_id: uuid.UUID, number: int, caller: Viewer) -> views.VersionSpec:  # pyright: ignore[reportUnusedFunction]
        return await service.version_spec(await env_of(caller), workflow_id, number)

    @router.post("/{workflow_id}/versions/{number}/restore")
    @speaks
    async def restore(workflow_id: uuid.UUID, number: int, caller: Member) -> views.Draft:  # pyright: ignore[reportUnusedFunction]
        return await service.restore(await env_of(caller), workflow_id, number)

    @router.get("/{workflow_id}/runs")
    @speaks
    async def runs(workflow_id: uuid.UUID, caller: Viewer) -> list[run_views.RunSummary]:  # pyright: ignore[reportUnusedFunction]
        return await service.list_runs(await env_of(caller), workflow_id)

    @router.post("/{workflow_id}/runs", status_code=201)
    @speaks
    async def start(  # pyright: ignore[reportUnusedFunction]
        workflow_id: uuid.UUID, body: StartBody, caller: Member
    ) -> run_views.RunSummary:
        return await service.start(
            await env_of(caller),
            workflow_id,
            body.inputs,
            test=body.test.model_dump() if body.test else None,
        )

    @router.get("/{workflow_id}/runs/{number}")
    @speaks
    async def run_by_number(  # pyright: ignore[reportUnusedFunction]
        workflow_id: uuid.UUID, number: int, caller: Viewer
    ) -> run_views.RunDetail:
        return await service.get_run_by_number(await env_of(caller), workflow_id, number)

    # ----- triggers and webhooks -----

    def base_url() -> str:
        return str(runtime.web.public_url)

    @router.get("/{workflow_id}/deliveries/last")
    @speaks
    async def last_delivery(workflow_id: uuid.UUID, caller: Viewer) -> webhooks.Delivery:  # pyright: ignore[reportUnusedFunction]
        return await webhooks.last_delivery(await env_of(caller), workflow_id)

    @router.get("/{workflow_id}/triggers/{key}/webhook")
    @speaks
    async def webhook(workflow_id: uuid.UUID, key: str, caller: Viewer) -> webhooks.WebhookInfo:  # pyright: ignore[reportUnusedFunction]
        return await webhooks.info(await env_of(caller), workflow_id, key, base_url=base_url())

    @router.get("/{workflow_id}/triggers/{key}/deliveries")
    @speaks
    async def deliveries(  # pyright: ignore[reportUnusedFunction]
        workflow_id: uuid.UUID, key: str, caller: Viewer
    ) -> list[webhooks.DeliveryRow]:
        return await webhooks.deliveries(await env_of(caller), workflow_id, key)

    @router.post("/{workflow_id}/triggers/{key}/preview")
    @speaks
    async def preview(  # pyright: ignore[reportUnusedFunction]
        workflow_id: uuid.UUID, key: str, body: webhooks.Preview, caller: Viewer
    ) -> dict[str, Any]:
        return await webhooks.preview(await env_of(caller), workflow_id, key, body)

    @router.post("/{workflow_id}/triggers/{key}/listen")
    @speaks
    async def listen(workflow_id: uuid.UUID, key: str, caller: Viewer) -> webhooks.WebhookInfo:  # pyright: ignore[reportUnusedFunction]
        env = await env_of(caller)
        return await webhooks.listen(env, workflow_id, key, on=True, base_url=base_url())

    @router.delete("/{workflow_id}/triggers/{key}/listen")
    @speaks
    async def unlisten(workflow_id: uuid.UUID, key: str, caller: Viewer) -> webhooks.WebhookInfo:  # pyright: ignore[reportUnusedFunction]
        env = await env_of(caller)
        return await webhooks.listen(env, workflow_id, key, on=False, base_url=base_url())

    @router.post("/{workflow_id}/triggers/{key}/rotate")
    @speaks
    async def rotate(  # pyright: ignore[reportUnusedFunction]
        workflow_id: uuid.UUID, key: str, body: webhooks.Rotate, caller: Member
    ) -> webhooks.Secret:
        return await webhooks.rotate(await env_of(caller), workflow_id, key, body)

    @router.post("/{workflow_id}/triggers/{key}/deliveries/{delivery}/replay", status_code=201)
    @speaks
    async def replay(  # pyright: ignore[reportUnusedFunction]
        workflow_id: uuid.UUID, key: str, delivery: str, caller: Member
    ) -> webhooks.DeliveryRow:
        return await webhooks.replay(await env_of(caller), workflow_id, key, delivery)

    return router
