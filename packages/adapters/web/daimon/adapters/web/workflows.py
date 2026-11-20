"""`/api/workflows/*`: the workflows module on the real stores.

Every rule lives in `daimon.core.workflows.service`, the one the chat tools use too; this
module describes who is asking and shapes the answers. Reading needs `workflows.view`,
everything else `workflows.run`; what a person may do to one workflow comes from their role
on it. Triggers, webhooks and the approvals inbox are not built yet and answer plainly.
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
from daimon.adapters.web.directory import chattable
from daimon.adapters.web.routines import reachable_channels_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.stores import tenants
from daimon.core.stores.channel_directory import list_shared_channel_ids
from daimon.core.workflows import catalog, run_views, service, templates, views
from daimon.core.workflows.access import Who
from daimon.core.workflows.runs import run_channel
from daimon.core.workflows.service import Env, WorkflowError
from daimon.core.workflows.taint import tool_writes
from daimon.core.workflows.validate import Validation
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

Viewer = Annotated[WebCaller, Depends(requires("workflows.view"))]
Member = Annotated[WebCaller, Depends(requires("workflows.run"))]

SOON = "This is not available yet."
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
            key = catalog.slug(name)
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
        return await service.attention(await env_of(caller))

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

    # ----- the approvals inbox (not built) -----

    @router.get("/inbox")
    async def inbox(caller: Viewer) -> list[None]:  # pyright: ignore[reportUnusedFunction]
        await env_of(caller)
        return []

    @router.get("/inbox/{item_id}")
    async def inbox_item(item_id: str, caller: Viewer) -> None:  # pyright: ignore[reportUnusedFunction]
        await env_of(caller)
        raise HTTPException(status_code=404, detail="There is nothing waiting here.")

    @router.post("/inbox/{item_id}/decide")
    async def inbox_decide(item_id: str, caller: Member) -> None:  # pyright: ignore[reportUnusedFunction]
        await env_of(caller)
        raise HTTPException(status_code=404, detail="There is nothing waiting here.")

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

    # ----- triggers and webhooks (not built) -----

    @router.get("/{workflow_id}/deliveries/last")
    async def last_delivery(workflow_id: uuid.UUID, caller: Viewer) -> None:  # pyright: ignore[reportUnusedFunction]
        await env_of(caller)
        raise HTTPException(status_code=404, detail="Nothing has arrived yet.")

    @router.get("/{workflow_id}/triggers/{trigger_id}/deliveries")
    async def deliveries(workflow_id: uuid.UUID, trigger_id: str, caller: Viewer) -> list[None]:  # pyright: ignore[reportUnusedFunction]
        await env_of(caller)
        return []

    @router.post("/{workflow_id}/triggers/{trigger_id}/preview")
    async def preview(workflow_id: uuid.UUID, trigger_id: str, caller: Viewer) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        await env_of(caller)
        return {"ok": False, "error": "Webhook triggers are not available yet."}

    for method, suffix in (
        ("GET", "webhook"),
        ("POST", "listen"),
        ("DELETE", "listen"),
        ("POST", "rotate"),
        ("POST", "deliveries/{delivery_id}/replay"),
    ):

        async def not_built(  # pyright: ignore[reportUnusedFunction]
            workflow_id: uuid.UUID, trigger_id: str, caller: Member
        ) -> None:
            await env_of(caller)
            raise HTTPException(status_code=404, detail="Webhook triggers are not available yet.")

        router.add_api_route(
            f"/{{workflow_id}}/triggers/{{trigger_id}}/{suffix}", not_built, methods=[method]
        )

    return router
