"""Authenticated C3 endpoints and a disconnect-independent turn supervisor."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

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
)
from pydantic import JsonValue, ValidationError

from .auth import Authenticator
from .errors import HarnessError
from .journal import ConflictError, Journal, identifier
from .model_adapter import AzureModelAdapter, ModelAdapter
from .runtime import TurnContext

audit_logger = logging.getLogger("daimon_harness.audit")
audit_logger.setLevel(logging.INFO)
if not audit_logger.handlers:
    audit_logger.addHandler(logging.StreamHandler())
audit_logger.propagate = False


class Supervisor:
    def __init__(self, home: Path, adapter: ModelAdapter) -> None:
        self.journal = Journal(home)
        self.home = home
        self.adapter = adapter
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.contexts: dict[str, TurnContext] = {}
        self.changed = asyncio.Event()

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

    async def run(self, request: TurnRequest) -> None:
        usage_count = 0
        failure: dict[str, JsonValue] | None = None

        def emit(kind: EventType, data: dict[str, JsonValue]) -> None:
            nonlocal usage_count, failure
            if kind == "usage":
                usage_count += 1
            if kind == "error":
                failure = {"kind": data["kind"], "message": data["message"]}
            self.journal.append(request, kind, data)
            self.changed.set()

        context = TurnContext(request, self.home, emit)
        self.contexts[request.turn_id] = context
        outcome = "completed"
        try:
            emit("status_running", {})
            async with asyncio.timeout(request.limits.deadline_s):
                history = await self.adapter.run(context, self.journal.history(request.session_id))
            if not usage_count:
                raise HarnessError("usage_missing", "Turn completed without measured usage")
            self.journal.save_history(request.session_id, history)
        except (asyncio.CancelledError, TimeoutError):
            outcome = "interrupted"
        except Exception as exc:
            outcome = "errored"
            # SDK exception messages can include credentials and request bodies.
            kind = exc.kind if isinstance(exc, HarnessError) else type(exc).__name__
            if failure is not None and isinstance(exc, HarnessError):
                failure = {"kind": kind, "message": str(exc)}
            if failure is None:
                emit("error", {"kind": kind, "message": "Harness turn failed", "retryable": False})
        finally:
            original_failure = failure
            for request_id in self.journal.pending_requests(request):
                emit(
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
            emit("turn_ended", data)
            self.contexts.pop(request.turn_id, None)
            self.changed.set()

    def submit(self, request: TurnRequest) -> None:
        self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
        if self.journal.claim(request):
            self.tasks[request.turn_id] = asyncio.create_task(self.run(request))

    async def stream(self, request: TurnRequest) -> AsyncIterator[str]:
        after = 0
        while True:
            self.changed.clear()
            events = self.journal.events(request.session_id, after)
            complete = self.journal.ended(request.turn_id)
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
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.journal.close()


def create_app(
    *,
    home: Path | None = None,
    token: str | None = None,
    adapter: ModelAdapter | None = None,
) -> FastAPI:
    """One worker per session sandbox; the HOME lease rejects accidental replicas."""
    root = home or Path.home()
    auth = Authenticator(token)
    supervisor = Supervisor(root, adapter or AzureModelAdapter())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with (supervisor.journal.root / "process.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            supervisor.recover()
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
            supervisor.submit(request)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Invalid session or turn identifier"
            ) from exc
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
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return CancelResponse(cancelled=True)

    async def events(session_id: str, after_seq: int = Query(default=0, ge=0)) -> EventsResponse:
        try:
            identifier(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid session identifier") from exc
        requests = supervisor.journal.turns(session_id)
        return EventsResponse(
            events=supervisor.journal.events(session_id, after_seq),
            complete=all(supervisor.journal.ended(r.turn_id) for r in requests),
        )

    async def invoke(
        envelope: InvocationEnvelope,
        caller: str = Depends(authenticate),
        accept: Annotated[str | None, Header()] = None,
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
                return await turns(TurnRequest.model_validate(route.body), caller, accept)
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
