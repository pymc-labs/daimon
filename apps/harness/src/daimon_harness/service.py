"""Authenticated C3 endpoints and a disconnect-independent turn supervisor."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Annotated, TypeVar, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from mux.drivers.selfhosted.wire import (
    ActionRequest,
    CancelResponse,
    EventsResponse,
    EventType,
    HealthResponse,
    InvocationEnvelope,
    TurnAccepted,
    TurnRequest,
    URLSkill,
)
from pydantic import JsonValue, ValidationError

from .auth import Authenticator
from .credentials import load_bindings, redact, turn_credentials
from .errors import HarnessError
from .isolation import can_switch_uid, prepare_service, protect_process
from .journal import ConflictError, Journal, identifier
from .model_adapter import AzureModelAdapter, ModelAdapter
from .runtime import TurnContext

T = TypeVar("T")

audit_logger = logging.getLogger("daimon_harness.audit")
audit_logger.setLevel(logging.INFO)
if not audit_logger.handlers:
    audit_logger.addHandler(logging.StreamHandler())
audit_logger.propagate = False


class Supervisor:
    def __init__(self, home: Path, adapter: ModelAdapter) -> None:
        self.operator_credentials = {
            name: value
            for name, value in os.environ.items()
            if name.startswith("HARNESS_MCP_CRED_") and name != "HARNESS_MCP_CRED_BINDINGS"
        }
        self.credential_bindings = load_bindings()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="harness-journal")
        self.submissions = asyncio.Lock()
        self.journal = Journal(home)
        self.home = home
        self.adapter = adapter
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.contexts: dict[str, TurnContext] = {}
        self.changed = asyncio.Event()

    async def io(self, operation: Callable[..., T], *args: object) -> T:
        # One worker owns all SQLite operations, including ordered event commits.
        future = asyncio.get_running_loop().run_in_executor(self.worker, partial(operation, *args))
        return await asyncio.shield(future)

    def recover(self) -> None:
        # A claimed turn can have executed tools. Never retry it after a process crash.
        for request in self.journal.turns():
            if not self.journal.ended(request.turn_id):
                self.journal.append(
                    request,
                    "error",
                    {
                        "kind": "process_interrupted",
                        "message": "Harness restarted during the turn",
                        "retryable": False,
                    },
                )
                for request_id in self.journal.pending_requests(request):
                    self.journal.append(
                        request,
                        "error",
                        {
                            "kind": "usage_unavailable",
                            "message": "Restart left model usage uncertain",
                            "retryable": False,
                            "request_id": request_id,
                        },
                    )
                self.journal.append(request, "turn_ended", {"outcome": "interrupted"})

    async def run(self, request: TurnRequest, credentials: dict[str, str]) -> None:
        usage_count = 0
        failure: dict[str, JsonValue] | None = None

        async def emit(kind: EventType, data: dict[str, JsonValue]) -> None:
            nonlocal usage_count, failure
            if kind == "usage":
                usage_count += 1
            if kind == "error":
                failure = {"kind": data["kind"], "message": data["message"]}
            await self.io(
                self.journal.append,
                request,
                kind,
                cast(dict[str, JsonValue], redact(data, credentials)),
            )
            self.changed.set()

        outcome = "completed"
        context: TurnContext | None = None
        try:
            sandbox, uid = await self.io(self.journal.sandbox, request.session_id)
            context = TurnContext(
                request,
                sandbox,
                emit,
                credentials=credentials,
                credential_bindings=self.credential_bindings,
                tool_uid=uid,
                state_dir=self.journal.root / "sessions" / request.session_id,
            )
            self.contexts[request.turn_id] = context
            await emit("status_running", {})
            previous = await self.io(self.journal.history, request.session_id)
            await self.io(
                self.journal.save_history,
                request.session_id,
                cast(
                    list[JsonValue],
                    redact(
                        previous
                        + [
                            {
                                "role": "user",
                                "content": "Interrupted turn input: "
                                + request.model_dump_json(include={"input"}),
                            }
                        ],
                        credentials,
                    ),
                ),
            )
            async with asyncio.timeout(request.limits.deadline_s):
                history = await self.adapter.run(context, previous)
            if not usage_count:
                raise HarnessError("usage_missing", "Turn completed without measured usage")
            await self.io(
                self.journal.save_history,
                request.session_id,
                cast(list[JsonValue], redact(history, credentials)),
            )
        except (asyncio.CancelledError, TimeoutError):
            outcome = "interrupted"
        except Exception as exc:
            outcome = "errored"
            # SDK exception messages can include credentials and request bodies.
            kind = exc.kind if isinstance(exc, HarnessError) else type(exc).__name__
            if isinstance(exc, HarnessError):
                failure = {"kind": kind, "message": str(exc)}
            if failure is None:
                await emit(
                    "error", {"kind": kind, "message": "Harness turn failed", "retryable": False}
                )
        finally:
            if context is not None:
                context.stop_processes()
            original_failure = failure
            for request_id in await self.io(self.journal.pending_requests, request):
                await emit(
                    "error",
                    {
                        "kind": "usage_unavailable",
                        "message": "Model request ended without usage",
                        "retryable": False,
                        "request_id": request_id,
                    },
                )
            if original_failure is not None:
                failure = original_failure
            data: dict[str, JsonValue] = {"outcome": outcome}
            if outcome == "errored":
                data["error"] = failure or {"kind": "harness", "message": "Harness turn failed"}
            await emit("turn_ended", data)
            credentials.clear()
            self.contexts.pop(request.turn_id, None)
            self.changed.set()

    async def submit(self, request: TurnRequest, mcp_authorization: str | None = None) -> None:
        async with self.submissions:
            if await self.io(self.journal.is_replay, request):
                return
            if any(isinstance(skill, URLSkill) for skill in request.skills):
                raise ValueError("URL skills are not supported; send an inline bundle")
            credentials = turn_credentials(
                request, mcp_authorization, self.operator_credentials, self.credential_bindings
            )
            self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
            await self.io(self.journal.sandbox, request.session_id)
            if await self.io(self.journal.claim, request):
                self.tasks[request.turn_id] = asyncio.create_task(self.run(request, credentials))

    async def stream(self, request: TurnRequest) -> AsyncIterator[str]:
        after = 0
        while True:
            self.changed.clear()
            complete = await self.io(self.journal.ended, request.turn_id)
            events = await self.io(self.journal.events, request.session_id, after)
            if not events and not complete:
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                continue
            for event in events:
                after = event.seq
                if event.turn_id == request.turn_id:
                    yield (
                        f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
                    )
            if complete:
                return

    async def close(self) -> None:
        for task in self.tasks.values():
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if isinstance(self.adapter, AzureModelAdapter) and self.adapter.credential is not None:
            await self.adapter.credential.close()
        await self.io(self.journal.close)
        self.worker.shutdown(wait=True)


def create_app(
    *,
    home: Path | None = None,
    token: str | None = None,
    adapter: ModelAdapter | None = None,
) -> FastAPI:
    """One worker per session sandbox; the HOME lease rejects accidental replicas."""
    root = home or Path.home()
    test_mode = os.environ.get("HARNESS_TEST_MODE") == "1"
    if test_mode:
        forbidden = {
            name
            for name in os.environ
            if (
                name
                in {
                    "HARNESS_TOKEN",
                    "AZURE_OPENAI_API_KEY",
                    "AZURE_CLIENT_SECRET",
                    "AZURE_CLIENT_CERTIFICATE_PATH",
                    "IDENTITY_HEADER",
                    "MSI_SECRET",
                }
                or (name.startswith("HARNESS_MCP_CRED_") and name != "HARNESS_MCP_CRED_BINDINGS")
            )
            and os.environ.get(name)
        }
        if adapter is None or isinstance(adapter, AzureModelAdapter) or forbidden:
            raise RuntimeError("test mode requires a fake adapter and no configured credentials")
    elif not can_switch_uid():
        raise RuntimeError("production requires separate service and tool UIDs")
    auth = Authenticator(token)
    supervisor = Supervisor(root, adapter or AzureModelAdapter())
    if not test_mode:
        prepare_service(root)
        protect_process()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with (supervisor.journal.root / "process.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await supervisor.io(supervisor.recover)
            try:
                yield
            finally:
                await supervisor.close()
                fcntl.flock(lease, fcntl.LOCK_UN)

    async def authenticate(
        authorization: Annotated[str | None, Header()] = None,
        x_agent_user_id: Annotated[str | None, Header()] = None,
        x_agent_foundry_call_id: Annotated[str | None, Header()] = None,
    ) -> str:
        return await auth.verify(authorization, x_agent_user_id, x_agent_foundry_call_id)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    secured = [Depends(authenticate)]
    app.state.supervisor = supervisor

    async def health() -> HealthResponse:
        return HealthResponse(version=os.environ.get("HARNESS_VERSION", "development"))

    async def turns(
        request: TurnRequest,
        caller: str = Depends(authenticate),
        accept: Annotated[str | None, Header()] = None,
        x_daimon_mcp_authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        audit_logger.info(
            json.dumps(
                {
                    "event": "harness.turn_authorized",
                    "caller": caller,
                    "session_id": request.session_id,
                    "turn_id": request.turn_id,
                }
            )
        )
        try:
            await supervisor.submit(request, x_daimon_mcp_authorization)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid turn configuration") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if accept and "application/json" in accept and "text/event-stream" not in accept:
            return JSONResponse(TurnAccepted(turn_id=request.turn_id).model_dump(), status_code=202)
        return StreamingResponse(
            supervisor.stream(request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def actions(turn_id: str, action: ActionRequest) -> dict[str, bool]:
        context = supervisor.contexts.get(turn_id)
        if context is None:
            raise HTTPException(status_code=404, detail="No active turn")
        prior = context.decisions.get(action.action_id)
        if prior is not None:
            if prior != action:
                raise HTTPException(status_code=409, detail="Action already decided differently")
            return {"accepted": True}
        future = context.pending.get(action.action_id)
        if future is None or future.done():
            raise HTTPException(status_code=404, detail="No pending action")
        context.decisions[action.action_id] = action
        future.set_result(action)
        return {"accepted": True}

    async def cancel(turn_id: str) -> CancelResponse:
        task = supervisor.tasks.get(turn_id)
        if task is None or task.done():
            return CancelResponse(cancelled=False)
        # Ensure a newly submitted task reaches its finally block before cancellation.
        await asyncio.sleep(0)
        if task.done():
            return CancelResponse(cancelled=False)
        if not task.cancelling():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return CancelResponse(cancelled=True)

    async def events(session_id: str, after_seq: int = Query(default=0, ge=0)) -> EventsResponse:
        try:
            identifier(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid session identifier") from exc
        complete = await supervisor.io(supervisor.journal.complete, session_id)
        return EventsResponse(
            events=await supervisor.io(supervisor.journal.events, session_id, after_seq),
            complete=complete,
        )

    async def invoke(
        envelope: InvocationEnvelope,
        caller: str = Depends(authenticate),
        accept: Annotated[str | None, Header()] = None,
        x_daimon_mcp_authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        route = envelope.c3
        method, path = route.method, route.path
        if route.query and not (
            method == "GET" and re.fullmatch(r"/v0/sessions/[^/]+/events", path)
        ):
            raise HTTPException(status_code=422, detail="Unexpected query parameters")
        if method == "GET" and route.body is not None:
            raise HTTPException(status_code=422, detail="GET body must be null")
        try:
            if method == "GET" and path == "/v0/healthz":
                return JSONResponse((await health()).model_dump())
            if method == "POST" and path == "/v0/turns":
                return await turns(
                    TurnRequest.model_validate(route.body),
                    caller,
                    accept,
                    x_daimon_mcp_authorization,
                )
            if method == "POST" and (
                match := re.fullmatch(r"/v0/turns/([^/]+)/(actions|cancel)", path)
            ):
                turn_id, operation = match.groups()
                if operation == "actions":
                    return JSONResponse(
                        await actions(turn_id, ActionRequest.model_validate(route.body))
                    )
                if route.body not in (None, {}):
                    raise HTTPException(status_code=422, detail="Cancel body must be empty")
                return JSONResponse((await cancel(turn_id)).model_dump())
            if method == "GET" and (match := re.fullmatch(r"/v0/sessions/([^/]+)/events", path)):
                if set(route.query) - {"after_seq"}:
                    raise HTTPException(status_code=422, detail="Unexpected query parameters")
                raw_after = route.query.get("after_seq", 0)
                if isinstance(raw_after, bool) or not isinstance(raw_after, (str, int)):
                    raise ValueError("invalid cursor")
                after = int(raw_after)
                if after < 0:
                    raise ValueError("negative cursor")
                return JSONResponse((await events(match[1], after)).model_dump(mode="json"))
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid C3 invocation") from exc
        raise HTTPException(status_code=404, detail="Unknown C3 route")

    async def readiness() -> dict[str, bool]:
        return {"ready": True}

    app.add_api_route("/v0/healthz", health, methods=["GET"], dependencies=secured)
    app.add_api_route("/v0/turns", turns, methods=["POST"], dependencies=secured)
    app.add_api_route(
        "/v0/turns/{turn_id}/actions", actions, methods=["POST"], dependencies=secured
    )
    app.add_api_route("/v0/turns/{turn_id}/cancel", cancel, methods=["POST"], dependencies=secured)
    app.add_api_route(
        "/v0/sessions/{session_id}/events", events, methods=["GET"], dependencies=secured
    )
    app.add_api_route("/invocations", invoke, methods=["POST"], dependencies=secured)
    app.add_api_route("/readiness", readiness, methods=["GET"])
    return app
