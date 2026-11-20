"""`/hooks/w/<trigger id>` and its `/test` twin: the public webhook ingress of workflows;
`/hooks/callback/...`: a run's signed callback URL, delivering to one Wait node.

Outside sign-in and the CSRF guard: the signature is the sender's credential, checked by
`ingress.receive` on the raw body. The body is read bounded here, refused by its declared
length before any of it is read and cut off as soon as it passes the cap, within a time
limit. Answers are small JSON objects that never carry a secret, and are never cached.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Final

from daimon.adapters.web.runtime import WebRuntime
from daimon.core.workflows import ingress, waits
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

READ_TIMEOUT_SECONDS: Final = 30.0
_HEADERS: Final = {"Cache-Control": "no-store"}


class _TooLarge(Exception):
    pass


async def _body(request: Request, cap: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > cap):
        raise _TooLarge
    chunks: list[bytes] = []
    size = 0
    async with asyncio.timeout(READ_TIMEOUT_SECONDS):
        async for chunk in request.stream():
            size += len(chunk)
            if size > cap:
                raise _TooLarge
            chunks.append(chunk)
    return b"".join(chunks)


def workflow_hooks_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter()

    async def receive(trigger_id: str, request: Request, *, test: bool) -> JSONResponse:
        cap = runtime.settings.workflows.webhook_max_body_bytes
        try:
            body = await _body(request, cap)
        except _TooLarge:
            return JSONResponse({"error": "too_large"}, status_code=413, headers=_HEADERS)
        except TimeoutError:
            return JSONResponse({"error": "too_slow"}, status_code=408, headers=_HEADERS)
        reply = await ingress.receive(
            runtime.sessionmaker,
            runtime.settings,
            trigger_id=trigger_id,
            test=test,
            headers={k.lower(): v for k, v in request.headers.items()},
            body=body,
            now=runtime.now(),
        )
        return JSONResponse(reply.body, status_code=reply.status, headers=_HEADERS)

    @router.post("/hooks/w/{trigger_id}", include_in_schema=False)
    async def live(trigger_id: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return await receive(trigger_id, request, test=False)

    @router.post("/hooks/w/{trigger_id}/test", include_in_schema=False)
    async def test(trigger_id: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        return await receive(trigger_id, request, test=True)

    @router.post("/hooks/callback/{run_id}/{node_id}/{signature}", include_in_schema=False)
    async def callback(  # pyright: ignore[reportUnusedFunction]
        run_id: uuid.UUID, node_id: str, signature: str, request: Request
    ) -> JSONResponse:
        try:
            body = await _body(request, runtime.settings.workflows.webhook_max_body_bytes)
        except _TooLarge:
            return JSONResponse({"error": "too_large"}, status_code=413, headers=_HEADERS)
        except TimeoutError:
            return JSONResponse({"error": "too_slow"}, status_code=408, headers=_HEADERS)
        payload = ingress.parse(body, request.headers.get("content-type", ""))
        if payload is None:
            return JSONResponse({"error": "bad_body"}, status_code=400, headers=_HEADERS)
        now = runtime.now()
        event = {"payload": payload, "received_at": now.isoformat()}
        taken = await waits.deliver_callback(
            runtime.sessionmaker,
            runtime.settings,
            run_id=run_id,
            node_id=node_id,
            signature=signature,
            payload=event,
            now=now,
        )
        if not taken:
            return JSONResponse({"error": "not_found"}, status_code=404, headers=_HEADERS)
        return JSONResponse({"ok": True}, status_code=202, headers=_HEADERS)

    return router
