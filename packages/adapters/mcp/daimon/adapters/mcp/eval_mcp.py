"""MCP for eval items, answered by the fake world.

- `/eval-mcp/{item}/{server}`: a third-party or relay MCP server of the case
  (`world.mcp_servers`), served over JSON-RPC to the item's world token.
- `answer_rpc`: the JSON-RPC answer shared with the connector and identity relays for eval
  callers, whose item is the one their credential's row is bound to (`EVAL_ITEM_CLAIM`).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Final

from daimon.adapters.mcp.auth.verifier import EVAL_ITEM_CLAIM
from daimon.core.evals.format import ExchangeResponse, McpTool
from daimon.core.evals.world.dispatch import FakeWorld, ItemNotRunningError
from daimon.core.evals.world.model import ItemRef
from pydantic import JsonValue
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.types import LATEST_PROTOCOL_VERSION

#: Tools only eval callers see.
EVAL_WORLD_TAG: Final = "eval-world"
EVAL_MCP_ROUTE: Final = "/eval-mcp/{item}/{server}"


def tool_texts(response: ExchangeResponse) -> list[str]:
    """A world answer's text blocks; a result without content becomes its JSON text."""
    texts = [b.text if b.text is not None else b.uri or "" for b in response.content or ()]
    if not texts and response.result is not None:
        result = response.result
        texts = [result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)]
    return texts


def tool_result(response: ExchangeResponse) -> dict[str, JsonValue]:
    """A world answer as an MCP `tools/call` result."""
    content: list[JsonValue] = [{"type": "text", "text": t} for t in tool_texts(response)]
    reply: dict[str, JsonValue] = {"content": content, "isError": response.is_error}
    if isinstance(response.result, dict):
        reply["structuredContent"] = response.result
    return reply


Rpc = dict[str, JsonValue]


def _envelope(
    request_id: JsonValue, result: JsonValue = None, error: str | None = None, code: int = -32601
) -> Rpc:
    if error is not None:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": error}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


async def answer_rpc(
    world: FakeWorld, item: ItemRef, server: str, message: dict[str, JsonValue]
) -> Rpc:
    """One JSON-RPC request to the case's MCP server `server`, answered by the world."""
    request_id, method = message.get("id"), message.get("method")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    recorded = next((s for s in item.case.world.mcp_servers or () if s.name == server), None)
    tools: list[McpTool] = list(recorded.tools or ()) if recorded else []
    if method == "initialize":
        info: dict[str, JsonValue] = {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": server, "version": "1"},
        }
        if recorded and recorded.instructions:
            info["instructions"] = recorded.instructions
        return _envelope(request_id, info)
    if method == "ping":
        return _envelope(request_id, {})
    if method == "tools/list":
        listed: list[JsonValue] = []
        for t in tools:
            entry: Rpc = {"name": t.name, "inputSchema": t.input_schema}
            if t.description:
                entry["description"] = t.description
            if t.output_schema:
                entry["outputSchema"] = t.output_schema
            if t.annotations:
                entry["annotations"] = t.annotations
            listed.append(entry)
        return _envelope(request_id, {"tools": listed})
    if method != "tools/call":
        return _envelope(request_id, error="Method not found")
    name, args = params.get("name"), params.get("arguments")
    if not isinstance(name, str):
        return _envelope(request_id, error="Invalid params", code=-32602)
    tool = next((t for t in tools if t.name == name), None)
    if tool is None:
        return _envelope(request_id, error=f"Unknown tool: {name}", code=-32602)
    answer = await world.mcp(item, server, name, args if isinstance(args, dict) else {}, tool)
    return _envelope(request_id, tool_result(answer.response))


def claimed_item(claims: Mapping[str, object]) -> uuid.UUID | None:
    """The eval item a verified credential's row is bound to, or None."""
    raw = claims.get(EVAL_ITEM_CLAIM)
    return _uuid(raw) if isinstance(raw, str) else None


async def answer_relayed(
    world: FakeWorld | None, claims: Mapping[str, object], server: str, message: JsonValue
) -> Response:
    """An eval caller's relayed JSON-RPC, answered by its item's world; 403 without one.
    Nothing goes upstream either way."""
    item_id = claimed_item(claims)
    item = await world.item(item_id) if world is not None and item_id else None
    if world is None or item is None:
        return Response(status_code=403)
    if not isinstance(message, dict):
        return JSONResponse(_envelope(None, error="Invalid request", code=-32600))
    if "id" not in message:
        return Response(status_code=202)
    try:
        return JSONResponse(await answer_rpc(world, item, server, message))
    except ItemNotRunningError:
        return Response(status_code=403)


def build_eval_mcp_route(world: FakeWorld) -> Callable[[Request], Awaitable[Response]]:
    async def eval_mcp(request: Request) -> Response:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        item = await world.item_for_token(token) if scheme.lower() == "bearer" and token else None
        if item is None or str(item.id) != str(_uuid(request.path_params["item"])):
            return Response(status_code=401)
        if request.method != "POST":
            return Response(status_code=405)
        try:
            message: JsonValue = json.loads(await request.body())
        except ValueError:
            return JSONResponse(_envelope(None, error="Parse error"))
        if not isinstance(message, dict):
            return JSONResponse(_envelope(None, error="Invalid request"))
        if "id" not in message:
            return Response(status_code=202)
        try:
            reply = await answer_rpc(world, item, str(request.path_params["server"]), message)
        except ItemNotRunningError:
            return Response(status_code=410)
        return JSONResponse(reply)

    return eval_mcp


def _uuid(raw: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None
