"""The fake world over HTTP, for sandbox gateways (served by the eval worker).

- `POST /world/{item}/http`: the gateway posts `{method, url, headers, body_b64, sni,
  client_uid}` and gets `{status, headers, body_b64, latency_ms, refused}`. `refused` means
  the world denied the call: the gateway closes the connection instead of answering.
- `/world/{item}/git/{host}/{path}`: raw git smart HTTP for the repo at `https://{host}/{path}`.

Both take the item's world token as a bearer token; a body over `MAX_BODY` is refused (413).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import random
import uuid
from typing import Final

from daimon.core.evals.world.dispatch import FakeWorld, ItemNotRunningError
from daimon.core.evals.world.model import HttpCall, ItemRef
from daimon.core.evals.world.services.git import find_repo
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

#: Headers the gateway recomputes for the bytes it sends.
_FRAMING: Final = frozenset({"content-length", "transfer-encoding", "content-encoding"})
_MAX_LATENCY_MS: Final = 30_000
MAX_BODY: Final = 64 << 20


async def capped_body(request: Request) -> bytes | None:
    """The request body, or None past `MAX_BODY`."""
    chunks, size = list[bytes](), 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


class GatewayRequest(BaseModel):
    method: str
    url: str
    headers: dict[str, str] = {}
    body_b64: str = ""
    sni: str | None = None
    client_uid: int | None = None


async def _item(world: FakeWorld, request: Request) -> ItemRef | Response:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    try:
        wanted = uuid.UUID(request.path_params["item"])
    except ValueError:
        return Response(status_code=404)
    item = await world.item_for_token(token) if scheme.lower() == "bearer" and token else None
    if item is None or item.id != wanted:
        return Response(status_code=401)
    return item


def _replay_latency(latency_ms: int | None) -> float:
    if not latency_ms:
        return 0.0
    return min(latency_ms, _MAX_LATENCY_MS) * random.uniform(0.8, 1.2) / 1000


def create_world_app(world: FakeWorld) -> Starlette:
    async def http(request: Request) -> Response:
        item = await _item(world, request)
        if isinstance(item, Response):
            return item
        if (raw := await capped_body(request)) is None:
            return Response(status_code=413)
        try:
            asked = GatewayRequest.model_validate_json(raw)
            body = base64.b64decode(asked.body_b64, validate=True)
        except (ValidationError, binascii.Error):
            return Response(status_code=400)
        call = HttpCall(asked.method.upper(), asked.url, asked.headers, body)
        try:
            answer = await world.http(item, call)
        except ItemNotRunningError:
            return Response(status_code=410)
        reply = answer.response.http
        status = reply.status if reply else 502
        headers = {k: v for k, v in ((reply.headers or {}) if reply else {}).items()
                   if k.lower() not in _FRAMING}  # fmt: skip
        delay = _replay_latency(answer.response.latency_ms if answer.origin == "recorded" else None)
        await asyncio.sleep(delay)
        return JSONResponse({
            "status": status, "headers": headers,
            "body_b64": base64.b64encode(answer.raw_body).decode(),
            "latency_ms": round(delay * 1000), "refused": answer.origin == "denied",
        })  # fmt: skip

    async def git(request: Request) -> Response:
        item = await _item(world, request)
        if isinstance(item, Response):
            return item
        query = f"?{request.url.query}" if request.url.query else ""
        url = f"https://{request.path_params['path']}{query}"
        if (raw := await capped_body(request)) is None:
            return Response(status_code=413)
        call = HttpCall(request.method, url, dict(request.headers), raw)
        if find_repo(item, call) is None:
            return Response(status_code=404)
        try:
            answer = await world.http(item, call)
        except ItemNotRunningError:
            return Response(status_code=410)
        reply = answer.response.http
        if reply is None:
            return Response(status_code=502)
        headers = {k: v for k, v in (reply.headers or {}).items() if k.lower() not in _FRAMING}
        return Response(answer.raw_body, status_code=reply.status, headers=headers)

    return Starlette(
        routes=[
            Route("/world/{item}/http", http, methods=["POST"]),
            Route("/world/{item}/git/{path:path}", git, methods=["GET", "POST"]),
        ]
    )
